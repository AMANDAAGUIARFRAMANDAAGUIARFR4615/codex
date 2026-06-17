"""向已登录的 claude.ai 页面提问，并从「网络原始数据」读取回答。

为什么不抓 DOM：claude.ai 把模型输出当 HTML 渲染——会吞掉 ``<tool_call>`` 这类标签、
混入「Thinking」思考链 UI、空内容时还会让“等回答稳定”一直空转到超时。真正干净、完整、
带明确结束信号的是 claude.ai 自己请求的 **completion 接口的原始 SSE 流**。

抓取方式：用 Playwright 的 ``page.on("response")`` 监听 completion 响应（不受页面 JS 世界 /
是否提前捕获 fetch 引用的影响，比在页面里包 ``window.fetch`` 更稳），生成结束后读取整段原始
SSE 作为权威回答；同时轮询会话消息 API 做「增量回显 + 结束判定」，让客户端能看到进度。

两层兜底：completion 原始 SSE 读不到 → 用会话消息 API 的文本；再不行 → 读 DOM。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from claude_sse import parse_completion_sse
from cookie_import import log
from debug_utils import save_debug

# ---------------------------------------------------------------------------
# 页面元素选择器（仅用于「发送问题」与 DOM 兜底；读回答走网络）
# ---------------------------------------------------------------------------

# claude.ai 的输入框（ProseMirror contenteditable），多写几个兜底选择器。
COMPOSER_SELECTORS = [
    'div[contenteditable="true"].ProseMirror',
    'div.ProseMirror[contenteditable="true"]',
    'div[contenteditable="true"]',
    "textarea",
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

# 记录原始数据的目录（GitHub Actions 会把 scripts/debug 作为产物上传）。
_DEBUG_DIR = Path("debug")
# 终端日志里预览多少字（完整内容写文件）；可用环境变量调大方便排查。
_PREVIEW = int(os.environ.get("CLAUDE_LOG_PREVIEW", "600") or "600")


def _is_completion_url(url: str) -> bool:
    """是否是 claude.ai 生成回答的 completion 接口（原始 SSE 来源）。"""
    if not url or "chat_conversations" not in url:
        return False
    return "/completion" in url or "completion?" in url


# ---------------------------------------------------------------------------
# 发送问题（DOM 操作；可靠，不涉及读回答）
# ---------------------------------------------------------------------------


def _first_visible(page, selectors: list[str], timeout_ms: int = 1000):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except Exception:  # noqa: BLE001
            continue
    return None


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
    except Exception:  # noqa: BLE001
        try:
            page.keyboard.insert_text(prompt)
        except Exception:  # noqa: BLE001
            page.keyboard.type(prompt)


def _send(page) -> None:
    button = _first_visible(page, SEND_BUTTON_SELECTORS, timeout_ms=1500)
    if button is not None:
        try:
            if button.is_enabled():
                button.click()
                return
        except Exception:  # noqa: BLE001
            pass
    page.keyboard.press("Enter")


def _is_generating(page) -> bool:
    return _first_visible(page, STOP_BUTTON_SELECTORS, timeout_ms=300) is not None


# ---------------------------------------------------------------------------
# 会话消息 API（增量回显 + 结束判定）+ DOM 兜底
# ---------------------------------------------------------------------------

# 取最后一条 assistant 消息的纯文本与 stop_reason（保留 <tool_call> 标记）。
_API_MSG_SCRIPT = """
async (url) => {
  try {
    const r = await fetch(url, { headers: { 'accept': 'application/json' }, credentials: 'include' });
    if (!r.ok) return { ok: false, status: r.status };
    const data = await r.json();
    const msgs = data.chat_messages || data.messages || [];
    const textOf = (m) => {
      if (Array.isArray(m.content)) {
        return m.content.filter((b) => b && b.type === 'text' && b.text)
          .map((b) => b.text).join('\\n').trim();
      }
      return (m.text || '').trim();
    };
    let last = null;
    for (const m of msgs) {
      const sender = m.sender || m.role || '';
      if (sender === 'assistant') last = m;
    }
    if (!last) return { ok: true, text: '', stop: '' };
    return { ok: true, text: textOf(last), stop: last.stop_reason || '' };
  } catch (e) { return { ok: false, error: String(e && e.message || e) }; }
}
"""

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


def _dom_latest(page) -> str:
    try:
        return (page.evaluate(_DOM_LATEST_SCRIPT) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _wait_conv_uuid(page, timeout: int = 25) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        match = _CONV_RE.search(page.url or "")
        if match:
            return match.group(1)
        page.wait_for_timeout(500)
    return ""


def _conv_url(org_uuid: str, conv_uuid: str) -> str:
    return (
        f"https://claude.ai/api/organizations/{org_uuid}"
        f"/chat_conversations/{conv_uuid}?tree=True&rendering_mode=messages"
    )


def _api_message(page, org_uuid: str, conv_uuid: str) -> tuple[str, str]:
    """返回 (最后一条 assistant 文本, stop_reason)。"""
    if not org_uuid or not conv_uuid:
        return "", ""
    try:
        result = page.evaluate(_API_MSG_SCRIPT, _conv_url(org_uuid, conv_uuid))
    except Exception:  # noqa: BLE001
        return "", ""
    if isinstance(result, dict) and result.get("ok"):
        return (result.get("text") or "").strip(), (result.get("stop") or "")
    return "", ""


def _api_text(page, org_uuid: str, conv_uuid: str) -> str:
    return _api_message(page, org_uuid, conv_uuid)[0]


def latest_api_text(page, org_uuid: str = "", timeout: int = 8) -> str:
    """取最后一条 assistant 的原始文本（会话消息 API，保留 <tool_call> 标记）。"""
    conv_uuid = _wait_conv_uuid(page, timeout=timeout)
    return _api_text(page, org_uuid, conv_uuid)


def _fallback_answer(page, org_uuid: str, on_delta, timeout: int) -> str:
    """读不到 completion 原始 SSE 时的兜底：轮询会话 API（DOM 再兜底）直到回答稳定。"""
    log("[claude] ⚠️ 未取到 completion 原始 SSE，回退到会话 API / DOM")
    conv_uuid = _wait_conv_uuid(page, timeout=15)
    deadline = time.time() + timeout
    last = ""
    source = ""
    stable = 0
    while time.time() < deadline:
        generating = _is_generating(page)
        text, stop = _api_message(page, org_uuid, conv_uuid)
        cur = "api"
        if not text:
            text = _dom_latest(page)
            cur = "dom"
        if text and text == last:
            stable += 1
            if stop or (not generating and stable >= 2):
                break
        else:
            stable = 0
            last = text
            source = cur
        page.wait_for_timeout(2000)
    if last:
        on_delta(last)
    _save_debug_text("last_answer.txt", last)
    _LAST_DEBUG.update(
        source=f"fallback:{source or '无'}", text=last, text_len=len(last), done=True
    )
    log(f"[claude] 兜底回答来源={source or '无'}，长度={len(last)} 字")
    return last


# ---------------------------------------------------------------------------
# 日志 + 最近一次请求的调试快照（供 /v1/debug/last 在线查看）
# ---------------------------------------------------------------------------

_LAST_DEBUG: dict = {}


def get_last_debug() -> dict:
    """返回最近一次提问的调试快照（发送的问题 + 返回的原始 SSE / 解析结果）。"""
    return dict(_LAST_DEBUG)


def _save_debug_text(name: str, text: str) -> None:
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (_DEBUG_DIR / name).write_text(text or "", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _preview(text: str, limit: int = _PREVIEW) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"…(共 {len(text)} 字)"


def log_outgoing(prompt: str) -> None:
    """打印 + 落盘「发送给 claude.ai 的问题」，方便排查。"""
    _save_debug_text("last_prompt.txt", prompt)
    _LAST_DEBUG.clear()
    _LAST_DEBUG.update(ts=time.time(), prompt=prompt, prompt_len=len(prompt))
    log(f"[claude] ▶ 发送问题（{len(prompt)} 字）: {_preview(prompt)}")


def _log_incoming(
    raw: str, text: str, thinking: str, stop: str, status: int, source: str
) -> None:
    """打印 + 落盘「claude.ai 返回的原始/解析数据」。"""
    _save_debug_text("last_completion.sse", raw)
    _save_debug_text("last_answer.txt", text)
    if thinking:
        _save_debug_text("last_thinking.txt", thinking)
    _LAST_DEBUG.update(
        source=source, status=status, done=True, stop=stop,
        raw_sse=raw, raw_len=len(raw), text=text, text_len=len(text),
        thinking=thinking, thinking_len=len(thinking),
    )
    log(
        f"[claude] ◀ 回复 source={source} status={status} stop={stop or '无'} "
        f"原始SSE={len(raw)}字 正文={len(text)}字 思考={len(thinking)}字"
    )
    log(f"[claude] ◀ 正文预览: {_preview(text)}")
    if os.environ.get("CLAUDE_LOG_RAW", "").lower() in ("1", "true", "yes"):
        log(f"[claude] ◀ 原始SSE预览: {_preview(raw)}")


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------


def stream_answer(
    page,
    prompt: str,
    on_delta,
    *,
    org_uuid: str = "",
    timeout: int = 240,
    poll_ms: int = 900,
    settle_s: float = 1.5,
) -> str:
    """发送 prompt，把回答增量回调给 on_delta(delta_str)，返回完整正文。

    回答的权威来源是 completion 接口的原始 SSE（``page.on("response")`` 抓取）；
    轮询会话消息 API 负责增量回显与结束判定；两者都拿不到时回退 DOM。
    """
    holder: dict = {"resp": None}

    def _on_response(resp) -> None:
        try:
            if holder["resp"] is None and _is_completion_url(resp.url):
                holder["resp"] = resp
        except Exception:  # noqa: BLE001
            pass

    page.on("response", _on_response)
    try:
        composer = _wait_composer(page)
        if composer is None:
            save_debug(page, "ask-no-composer")
            raise RuntimeError("未找到 claude.ai 输入框（可能未登录或页面结构变化）。")

        log_outgoing(prompt)
        _fill_prompt(page, composer, prompt)
        page.wait_for_timeout(150)
        _send(page)

        conv_uuid = _wait_conv_uuid(page, timeout=20)

        # 轮询会话 API：增量回显 + 结束判定（文本稳定且“停止”按钮消失，或拿到 stop_reason）。
        prev = ""
        last_grow = time.time()
        deadline = time.time() + timeout
        while time.time() < deadline:
            text, stop = _api_message(page, org_uuid, conv_uuid)
            if not text:
                text = _dom_latest(page)
            if len(text) > len(prev):
                on_delta(text[len(prev):])
                prev = text
                last_grow = time.time()
            generating = _is_generating(page)
            idle = time.time() - last_grow
            if prev and stop:
                break
            if prev and not generating and idle >= settle_s:
                break
            if prev and idle >= 25:  # 兜底：长时间不增长也不再傻等
                break
            page.wait_for_timeout(poll_ms)

        # 读取 completion 原始 SSE（此时一般已结束，read 立即返回）作为权威回答 + 落盘。
        raw = ""
        status = 0
        resp = holder["resp"]
        if resp is not None:
            try:
                status = resp.status
                raw = resp.text() or ""
            except Exception as exc:  # noqa: BLE001
                log(f"[claude] 读取 completion 响应体失败: {exc}")

        sse_text, thinking, sse_stop = parse_completion_sse(raw)
        final = sse_text or prev
        source = "network-sse" if sse_text else ("network-api" if prev else "empty")
        if len(final) > len(prev):  # SSE 比轮询到的更全 → 补发增量
            on_delta(final[len(prev):])

        _log_incoming(raw, final, thinking, sse_stop, status, source)

        if not final:
            return _fallback_answer(page, org_uuid, on_delta, timeout=60)
        return final
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:  # noqa: BLE001
            pass


def ask_claude(page, prompt: str, org_uuid: str = "", timeout: int = 240) -> str:
    """提问并返回完整回答（单次提问模式用，内部走与流式相同的网络抓取）。"""
    answer = stream_answer(page, prompt, lambda _p: None, org_uuid=org_uuid, timeout=timeout)
    save_debug(page, "claude-answer")
    if not answer:
        save_debug(page, "ask-empty-answer")
        raise RuntimeError("已发送问题但未抓取到回答内容。")
    return answer
