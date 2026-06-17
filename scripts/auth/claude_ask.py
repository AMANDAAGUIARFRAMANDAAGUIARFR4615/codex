"""在已登录的 claude.ai 页面上发送一个问题并抓取回答。

调用前提：login.py 已经完成 Cookie 导入、过完 Cloudflare，并已把页面导航到一个
可以开始新对话的 claude.ai 页面（导航与 Cloudflare 处理都由 login.py 负责）。
抓取回答用两条路：
1. DOM：等回答流式输出结束（"停止"按钮消失 + 文本稳定）后读最后一条助手消息。
2. API 兜底：用页面内 fetch 调 claude.ai 内部接口，按 conversation 取最后一条
   assistant 消息的纯文本（结构化、最完整）。
两者取非空且更完整的一个。
"""

from __future__ import annotations

import re
import time

from cookie_import import log
from debug_utils import save_debug

# claude.ai 的输入框（ProseMirror contenteditable），多写几个兜底选择器。
COMPOSER_SELECTORS = [
    'div[contenteditable="true"].ProseMirror',
    'div.ProseMirror[contenteditable="true"]',
    'div[contenteditable="true"]',
    'textarea',
]
SEND_BUTTON_SELECTORS = [
    'button[aria-label="Send message"]',
    'button[aria-label*="Send" i]',
    'button[data-testid="send-button"]',
    'fieldset button[type="submit"]',
]
# 生成中会出现“停止”按钮；它消失代表本轮回答结束。
STOP_BUTTON_SELECTORS = [
    'button[aria-label="Stop response"]',
    'button[aria-label*="Stop" i]',
    'button[data-testid="stop-button"]',
]

_CONV_RE = re.compile(r"/chat/([0-9a-fA-F-]{36})")

# 助手回答容器：claude.ai 现用 .font-claude-response（旧版 .font-claude-message）。
# 这个容器在流式输出时会逐字增长，可用于实时抓取。
_RESPONSE_SELECTOR = ".font-claude-response, .font-claude-message"

# 取页面上最后一条助手消息的纯文本。
_DOM_LATEST_SCRIPT = """
() => {
  const pick = (sel) => Array.from(document.querySelectorAll(sel));
  let nodes = pick('.font-claude-response, .font-claude-message');
  if (!nodes.length) nodes = pick('[data-testid="message-content"]');
  if (!nodes.length) return '';
  const el = nodes[nodes.length - 1];
  return (el.innerText || el.textContent || '').trim();
}
"""

# 当前助手回答个数（发问前后对比，确认新回答已出现）。
_COUNT_RESPONSES_SCRIPT = (
    "() => document.querySelectorAll('.font-claude-response, .font-claude-message').length"
)

# 取“最后一条且序号大于 base”的回答原文（未出现新回答时返回 null），流式轮询用。
_LATEST_RESPONSE_SCRIPT = """
(base) => {
  const els = document.querySelectorAll('.font-claude-response, .font-claude-message');
  if (els.length <= base) return null;
  const el = els[els.length - 1];
  return el.innerText || el.textContent || '';
}
"""

# 页面内调用 claude.ai 内部 API 取某个 conversation 的最后一条 assistant 文本。
_API_LATEST_SCRIPT = """
async (url) => {
  try {
    const r = await fetch(url, {
      headers: { 'accept': 'application/json' },
      credentials: 'include',
    });
    if (!r.ok) return { ok: false, status: r.status };
    const data = await r.json();
    const msgs = data.chat_messages || data.messages || [];
    const textOf = (m) => {
      if (Array.isArray(m.content)) {
        return m.content
          .filter((b) => b && b.type === 'text' && b.text)
          .map((b) => b.text)
          .join('\\n')
          .trim();
      }
      return (m.text || '').trim();
    };
    let last = '';
    for (const m of msgs) {
      const sender = m.sender || m.role || '';
      if (sender === 'assistant') {
        const t = textOf(m);
        if (t) last = t;
      }
    }
    return { ok: true, text: last, count: msgs.length };
  } catch (e) {
    return { ok: false, error: String(e && e.message || e) };
  }
}
"""


def _first_visible(page, selectors: list[str], timeout_ms: int = 1000):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except Exception:
            continue
    return None


def _is_generating(page) -> bool:
    return _first_visible(page, STOP_BUTTON_SELECTORS, timeout_ms=300) is not None


def _dom_latest(page) -> str:
    try:
        return (page.evaluate(_DOM_LATEST_SCRIPT) or "").strip()
    except Exception:
        return ""


def _wait_composer(page, timeout: int = 40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        composer = _first_visible(page, COMPOSER_SELECTORS, timeout_ms=1000)
        if composer is not None:
            return composer
        page.wait_for_timeout(1000)
    return None


def _fill_prompt(page, composer, prompt: str) -> None:
    composer.click()
    page.wait_for_timeout(200)
    try:
        composer.fill(prompt)
    except Exception:
        # contenteditable 偶尔不支持 fill，退回键盘输入。
        try:
            page.keyboard.insert_text(prompt)
        except Exception:
            page.keyboard.type(prompt)


def _send(page) -> None:
    button = _first_visible(page, SEND_BUTTON_SELECTORS, timeout_ms=1500)
    if button is not None:
        try:
            if button.is_enabled():
                button.click()
                return
        except Exception:
            pass
    # 兜底：聚焦输入框后回车发送（claude 用 Enter 发送、Shift+Enter 换行）。
    page.keyboard.press("Enter")


def _wait_started(page, timeout: int = 25) -> None:
    """等待这轮回答开始：出现“停止”按钮 / URL 变成 /chat/ / 出现助手消息。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_generating(page):
            return
        if _CONV_RE.search(page.url or ""):
            return
        if _dom_latest(page):
            return
        page.wait_for_timeout(500)


def _wait_conv_uuid(page, timeout: int = 25) -> str:
    """等待 URL 变成 /chat/<uuid>，拿到 conversation id 供 API 取回答。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        match = _CONV_RE.search(page.url or "")
        if match:
            return match.group(1)
        page.wait_for_timeout(500)
    return ""


def _api_text(page, org_uuid: str, conv_uuid: str) -> str:
    """安静地用页面内 fetch 取最后一条 assistant 文本（轮询用，不打日志）。"""
    if not org_uuid or not conv_uuid:
        return ""
    url = (
        f"https://claude.ai/api/organizations/{org_uuid}"
        f"/chat_conversations/{conv_uuid}?tree=True&rendering_mode=messages"
    )
    try:
        result = page.evaluate(_API_LATEST_SCRIPT, url)
    except Exception:
        return ""
    if isinstance(result, dict) and result.get("ok"):
        return (result.get("text") or "").strip()
    return ""


def _wait_answer(page, org_uuid: str, conv_uuid: str, timeout: int = 240) -> tuple[str, str]:
    """轮询直到回答稳定（优先用 API，DOM 兜底），返回 (answer, source)。

    完成判据：文本非空、连续两次读取一致，且（若能检测到）"停止"按钮已消失。
    基于文本稳定而非具体消息选择器，避免 claude.ai 改版导致一直等到超时。
    """
    deadline = time.time() + timeout
    last = ""
    source = ""
    stable = 0

    while time.time() < deadline:
        generating = _is_generating(page)

        text = _api_text(page, org_uuid, conv_uuid)
        cur_source = "api"
        if not text:
            text = _dom_latest(page)
            cur_source = "dom"

        if text and text == last:
            stable += 1
            if not generating and stable >= 2:
                return text, source
        else:
            stable = 0
            last = text
            source = cur_source

        page.wait_for_timeout(2500)

    log(f"[ask] ⚠️ 等待回答超时（{timeout}s），返回当前已抓到的文本")
    return last, source


def ask_claude(page, prompt: str, org_uuid: str = "", timeout: int = 240) -> str:
    """在当前 claude.ai 页面提问并返回回答文本（页面需已就绪、可开始新对话）。"""
    composer = _wait_composer(page)
    if composer is None:
        save_debug(page, "ask-no-composer")
        raise RuntimeError("未找到 claude.ai 输入框（可能未登录或页面结构变化）。")

    log(f"[ask] 输入问题（{len(prompt)} 字）并发送...")
    _fill_prompt(page, composer, prompt)
    page.wait_for_timeout(300)
    _send(page)

    _wait_started(page)
    conv_uuid = _wait_conv_uuid(page)
    log(f"[ask] 对话 ID: {conv_uuid or '未知'}，等待回答输出结束...")

    answer, source = _wait_answer(page, org_uuid, conv_uuid, timeout=timeout)

    save_debug(page, "claude-answer")
    log(f"[ask] 回答来源={source or '无'}，长度={len(answer)} 字")
    if not answer:
        save_debug(page, "ask-empty-answer")
        raise RuntimeError("已发送问题但未抓取到回答内容。")
    return answer


def _count_responses(page) -> int:
    try:
        return int(page.evaluate(_COUNT_RESPONSES_SCRIPT) or 0)
    except Exception:
        return 0


def _latest_response(page, base: int) -> str:
    try:
        text = page.evaluate(_LATEST_RESPONSE_SCRIPT, base)
    except Exception:
        return ""
    return text or ""


def stream_answer(
    page,
    prompt: str,
    on_delta,
    *,
    org_uuid: str = "",
    timeout: int = 240,
    poll_ms: int = 300,
    settle_s: float = 1.5,
) -> str:
    """发送 prompt 并把回答“增量”实时回调给 on_delta(delta_str)，返回完整回答。

    通过轮询最后一条 .font-claude-response 的 innerText 实现流式：每次只把新增部分
    交给 on_delta。判定结束：文本不再增长且“停止”按钮已消失并稳定 settle_s 秒。
    DOM 一直抓不到时，用内部 API 兜底一次性返回全文。
    """
    composer = _wait_composer(page)
    if composer is None:
        save_debug(page, "ask-no-composer")
        raise RuntimeError("未找到 claude.ai 输入框（可能未登录或页面结构变化）。")

    base = _count_responses(page)
    _fill_prompt(page, composer, prompt)
    page.wait_for_timeout(200)
    _send(page)
    _wait_started(page)

    prev = ""
    last_grow = time.time()
    deadline = time.time() + timeout

    while time.time() < deadline:
        text = _latest_response(page, base)
        if len(text) > len(prev):
            on_delta(text[len(prev):])
            prev = text
            last_grow = time.time()
        else:
            idle = time.time() - last_grow
            generating = _is_generating(page)
            if prev and not generating and idle >= settle_s:
                break
            if prev and idle >= 12:  # 兜底：长时间不增长也不再傻等
                break
        page.wait_for_timeout(poll_ms)

    if not prev:
        conv_uuid = _wait_conv_uuid(page, timeout=8)
        api_text = _api_text(page, org_uuid, conv_uuid)
        if api_text:
            on_delta(api_text)
            prev = api_text

    return prev
