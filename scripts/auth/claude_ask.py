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

# 取页面上最后一条助手消息的纯文本。
_DOM_LATEST_SCRIPT = """
() => {
  const pick = (sel) => Array.from(document.querySelectorAll(sel));
  let nodes = pick('.font-claude-message');
  if (!nodes.length) nodes = pick('[data-testid="message-content"]');
  if (!nodes.length) return '';
  const el = nodes[nodes.length - 1];
  return (el.innerText || el.textContent || '').trim();
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


def _wait_done(page, timeout: int = 240) -> str:
    """轮询直到回答输出结束（停止按钮消失且文本稳定），返回 DOM 抓到的文本。"""
    start = time.time()
    deadline = start + timeout
    last_text = ""
    last_change = start
    seen_generating = False

    while time.time() < deadline:
        generating = _is_generating(page)
        seen_generating = seen_generating or generating

        text = _dom_latest(page)
        if text != last_text:
            last_text = text
            last_change = time.time()

        idle = time.time() - last_change
        if last_text and not generating and idle >= 2.5 and (seen_generating or time.time() - start > 8):
            return last_text

        page.wait_for_timeout(700)

    log(f"[ask] ⚠️ 等待回答超时（{timeout}s），返回当前已抓到的文本")
    return last_text


def _api_latest(page, org_uuid: str, conv_uuid: str) -> str:
    if not org_uuid or not conv_uuid:
        return ""
    url = (
        f"https://claude.ai/api/organizations/{org_uuid}"
        f"/chat_conversations/{conv_uuid}?tree=True&rendering_mode=messages"
    )
    try:
        result = page.evaluate(_API_LATEST_SCRIPT, url)
    except Exception as exc:  # noqa: BLE001
        log(f"[ask] API 兜底取回答异常: {exc}")
        return ""
    if not isinstance(result, dict) or not result.get("ok"):
        log(f"[ask] API 兜底未取到回答: {result}")
        return ""
    return (result.get("text") or "").strip()


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
    log("[ask] 回答生成中，等待输出结束...")
    dom_text = _wait_done(page, timeout=timeout)

    conv_match = _CONV_RE.search(page.url or "")
    conv_uuid = conv_match.group(1) if conv_match else ""
    api_text = _api_latest(page, org_uuid, conv_uuid)

    save_debug(page, "claude-answer")

    log(f"[ask] DOM 文本 {len(dom_text)} 字，API 文本 {len(api_text)} 字")
    answer = api_text or dom_text
    if not answer:
        save_debug(page, "ask-empty-answer")
        raise RuntimeError("已发送问题但未抓取到回答内容。")
    return answer
