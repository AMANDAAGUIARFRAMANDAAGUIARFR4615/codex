"""向已登录的 claude.ai 页面提问，并从「网络原始数据」读取回答。

为什么不抓 DOM：claude.ai 把模型输出当 HTML 渲染——会吞掉 ``<tool_call>`` 这类标签、
混入「Thinking」思考链 UI、空内容时还会让“等回答稳定”一直空转到超时。真正干净、完整、
带明确结束信号（流关闭）的是 claude.ai 自己请求的 **completion 接口的原始 SSE 流**。

抓取方式：在页面「主世界」里包裹 ``window.fetch``，把 completion 响应 ``clone()`` 一份，
边到达边把原文累积进 ``window.__claudeCaps``；Python 侧轮询它实现增量流式 + 完成判定。

patchright 注意：``add_init_script`` 跑在「隔离世界」，页面看不到它改的 ``window.fetch``，
所以 hook 必须以 ``<script>`` 标签注入「主世界」（与 capsolver 的 turnstile hook 同一套路）。
``page.evaluate`` 在 patchright 下跑在主世界，用于二次补装与读取抓到的数据。

仍保留两层兜底：completion 抓取失败 → 轮询会话消息 API → 读 DOM。
"""

from __future__ import annotations

import json
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
# 生成中会出现“停止”按钮；它消失代表本轮回答结束（DOM 兜底用）。
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


# ---------------------------------------------------------------------------
# 网络抓取 hook（主世界）
# ---------------------------------------------------------------------------

# 主世界里运行：包裹 window.fetch，把 completion 响应 clone 一份边到达边累积。
# 同时把所有 /api/ 请求 URL 记进 window.__claudeNet，抓取失败时用于定位真实端点。
_CAPTURE_MAIN_JS = r"""
(() => {
  if (window.__claudeCapInstalled) return;
  window.__claudeCapInstalled = true;
  window.__claudeCaps = window.__claudeCaps || [];
  window.__claudeNet = window.__claudeNet || [];
  window.__claudeCapSeq = window.__claudeCapSeq || 0;
  const MAX = 60;
  const isCompletion = (u) => {
    try {
      return /chat_conversations\/[^/]+\/(retry_)?completion(\b|\?|$)/.test(u)
          || /\/(retry_)?completion(\?|$)/.test(u) && /chat_conversations/.test(u);
    } catch (e) { return false; }
  };
  const orig = window.fetch;
  window.fetch = function (input, init) {
    let url = '';
    try { url = (input && typeof input === 'object' && 'url' in input) ? input.url : ('' + input); }
    catch (e) {}
    const promise = orig.apply(this, arguments);
    try {
      if (typeof url === 'string' && url.indexOf('/api/') !== -1) {
        window.__claudeNet.push(url);
        while (window.__claudeNet.length > 40) window.__claudeNet.shift();
      }
      if (isCompletion(url)) {
        const cap = { id: ++window.__claudeCapSeq, url: url, ts: Date.now(),
                      done: false, status: 0, raw: '', error: '' };
        window.__claudeCaps.push(cap);
        while (window.__claudeCaps.length > MAX) window.__claudeCaps.shift();
        promise.then((resp) => {
          cap.status = resp.status;
          let clone;
          try { clone = resp.clone(); }
          catch (e) { cap.error = 'clone:' + e; cap.done = true; return; }
          const body = clone.body;
          if (!body || !body.getReader) { cap.done = true; return; }
          const reader = body.getReader();
          const dec = new TextDecoder('utf-8');
          const pump = () => reader.read().then((res) => {
            if (res.done) { cap.raw += dec.decode(); cap.done = true; return; }
            try { cap.raw += dec.decode(res.value, { stream: true }); } catch (e) {}
            return pump();
          }).catch((e) => { cap.error = 'read:' + e; cap.done = true; });
          pump();
        }).catch((e) => { cap.error = 'fetch:' + e; cap.done = true; });
      }
    } catch (e) {}
    return promise;
  };
})();
"""

# 隔离世界 → 主世界：init script 注入一个 <script> 标签，让 hook 在 document-start 生效。
_CAPTURE_BOOTSTRAP = (
    """
(() => {
  if (window.__claudeCapBootstrapped) return;
  window.__claudeCapBootstrapped = true;
  const SRC = %s;
  const inject = () => {
    try {
      const root = document.documentElement || document.head || document.body;
      if (!root) return false;
      const s = document.createElement('script');
      s.textContent = SRC;
      root.appendChild(s);
      s.remove();
      return true;
    } catch (e) { return false; }
  };
  if (!inject()) {
    const t = setInterval(() => { if (inject()) clearInterval(t); }, 5);
    setTimeout(() => clearInterval(t), 8000);
  }
})();
"""
    % json.dumps(_CAPTURE_MAIN_JS)
)

_CAP_SEQ_JS = "() => (window.__claudeCapSeq || 0)"
_RECENT_NET_JS = "() => (window.__claudeNet || []).slice(-20)"

# 取「id 大于 afterId 的最早一条」capture，只回传未消费的增量（避免每轮重传整段）。
# 注意：page.evaluate 只传一个参数，故用数组解构 [afterId, consumed]。
_CAP_NEW_JS = """
([afterId, consumed]) => {
  const caps = window.__claudeCaps || [];
  let best = null;
  for (const c of caps) { if (c.id > afterId && (!best || c.id < best.id)) best = c; }
  if (!best) return null;
  const raw = best.raw || '';
  return {
    id: best.id, url: best.url, done: !!best.done, status: best.status || 0,
    error: best.error || '', total: raw.length, chunk: raw.slice(consumed),
  };
}
"""


def install_capture(target) -> None:
    """在 BrowserContext / Page 上注入 completion 抓取 hook（页面脚本执行前生效）。"""
    try:
        target.add_init_script(_CAPTURE_BOOTSTRAP)
        log("[claude] 已注入 completion 网络抓取 hook")
    except Exception as exc:  # noqa: BLE001
        log(f"[claude] ⚠️ 注入网络抓取 hook 失败: {exc}")


def ensure_capture(page) -> None:
    """二次补装：page.evaluate 在主世界执行且幂等，导航后调用确保 hook 已就位。"""
    try:
        page.evaluate(_CAPTURE_MAIN_JS)
    except Exception:  # noqa: BLE001
        pass


def _capture_seq(page) -> int:
    try:
        return int(page.evaluate(_CAP_SEQ_JS) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _read_capture(page, after_id: int, consumed: int) -> dict | None:
    try:
        return page.evaluate(_CAP_NEW_JS, [after_id, consumed])
    except Exception:  # noqa: BLE001
        return None


def _recent_api_urls(page) -> list[str]:
    try:
        return list(page.evaluate(_RECENT_NET_JS) or [])
    except Exception:  # noqa: BLE001
        return []


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


# ---------------------------------------------------------------------------
# 会话消息 API + DOM 兜底
# ---------------------------------------------------------------------------

_API_LATEST_SCRIPT = """
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
    let last = '';
    for (const m of msgs) {
      const sender = m.sender || m.role || '';
      if (sender === 'assistant') { const t = textOf(m); if (t) last = t; }
    }
    return { ok: true, text: last, count: msgs.length };
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


def _is_generating(page) -> bool:
    return _first_visible(page, STOP_BUTTON_SELECTORS, timeout_ms=300) is not None


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


def _api_text(page, org_uuid: str, conv_uuid: str) -> str:
    if not org_uuid or not conv_uuid:
        return ""
    url = (
        f"https://claude.ai/api/organizations/{org_uuid}"
        f"/chat_conversations/{conv_uuid}?tree=True&rendering_mode=messages"
    )
    try:
        result = page.evaluate(_API_LATEST_SCRIPT, url)
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(result, dict) and result.get("ok"):
        return (result.get("text") or "").strip()
    return ""


def latest_api_text(page, org_uuid: str = "", timeout: int = 8) -> str:
    """取最后一条 assistant 的原始文本（会话消息 API，保留 <tool_call> 标记）。"""
    conv_uuid = _wait_conv_uuid(page, timeout=timeout)
    return _api_text(page, org_uuid, conv_uuid)


def _fallback_answer(page, org_uuid: str, on_delta, timeout: int) -> str:
    """没抓到 completion 网络流时的兜底：轮询会话 API（DOM 再兜底）直到回答稳定。"""
    log("[claude] ⚠️ 未抓到 completion 网络流，回退到会话 API / DOM")
    conv_uuid = _wait_conv_uuid(page, timeout=15)
    deadline = time.time() + timeout
    last = ""
    source = ""
    stable = 0
    while time.time() < deadline:
        generating = _is_generating(page)
        text = _api_text(page, org_uuid, conv_uuid)
        cur = "api"
        if not text:
            text = _dom_latest(page)
            cur = "dom"
        if text and text == last:
            stable += 1
            if not generating and stable >= 2:
                break
        else:
            stable = 0
            last = text
            source = cur
        page.wait_for_timeout(2000)
    if last:
        on_delta(last)
    log(f"[claude] 兜底回答来源={source or '无'}，长度={len(last)} 字")
    return last


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


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
    log(f"[claude] ▶ 发送问题（{len(prompt)} 字）: {_preview(prompt)}")


def _log_incoming(raw: str, text: str, thinking: str, stop: str, status: int, done: bool) -> None:
    """打印 + 落盘「claude.ai 返回的原始/解析数据」。"""
    _save_debug_text("last_completion.sse", raw)
    _save_debug_text("last_answer.txt", text)
    if thinking:
        _save_debug_text("last_thinking.txt", thinking)
    log(
        f"[claude] ◀ 网络回复 status={status} done={done} stop={stop or '无'} "
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
    appear_timeout: int = 35,
    poll_ms: int = 300,
) -> str:
    """发送 prompt，并把回答「增量」实时回调给 on_delta(delta_str)，返回完整正文。

    回答来源是 completion 接口的原始 SSE（``window.__claudeCaps``）；抓不到时回退会话 API/DOM。
    """
    ensure_capture(page)
    composer = _wait_composer(page)
    if composer is None:
        save_debug(page, "ask-no-composer")
        raise RuntimeError("未找到 claude.ai 输入框（可能未登录或页面结构变化）。")

    log_outgoing(prompt)
    after_id = _capture_seq(page)
    _fill_prompt(page, composer, prompt)
    page.wait_for_timeout(150)
    _send(page)

    # 1) 等 completion 网络流出现
    appear_deadline = time.time() + appear_timeout
    cap = None
    while time.time() < appear_deadline:
        cap = _read_capture(page, after_id, 0)
        if cap:
            break
        page.wait_for_timeout(300)

    if not cap:
        urls = _recent_api_urls(page)
        if urls:
            log("[claude] 最近的 /api/ 请求（用于定位 completion 端点）:")
            for u in urls[-8:]:
                log(f"[claude]   - {u[:140]}")
        return _fallback_answer(page, org_uuid, on_delta, timeout)

    log(f"[claude] 跟随 completion 网络流: {cap.get('url', '')[:110]}")

    # 2) 跟随流：只取增量、解析正文、回调 delta，直到流关闭
    raw = ""
    consumed = 0
    prev_text = ""
    last_grow = time.time()
    deadline = time.time() + timeout
    while time.time() < deadline:
        cap = _read_capture(page, after_id, consumed)
        if cap is None:
            break
        chunk = cap.get("chunk") or ""
        if chunk:
            raw += chunk
            consumed = cap.get("total", len(raw))
            text, _think, _stop = parse_completion_sse(raw)
            if len(text) > len(prev_text):
                on_delta(text[len(prev_text):])
                prev_text = text
                last_grow = time.time()
        if cap.get("done"):
            break
        if raw and time.time() - last_grow > 45:  # 极端兜底：长时间无新数据
            log("[claude] ⚠️ completion 流长时间无新数据，提前结束跟随")
            break
        page.wait_for_timeout(poll_ms)

    text, thinking, stop = parse_completion_sse(raw)
    if len(text) > len(prev_text):
        on_delta(text[len(prev_text):])
    _log_incoming(raw, text, thinking, stop, cap.get("status", 0) if cap else 0,
                  bool(cap.get("done")) if cap else False)

    if not text:  # 抓到了流但解析为空（格式异常）→ 兜底
        log("[claude] ⚠️ completion 流解析为空，回退会话 API/DOM")
        return _fallback_answer(page, org_uuid, on_delta, timeout=60)

    return text


def ask_claude(page, prompt: str, org_uuid: str = "", timeout: int = 240) -> str:
    """提问并返回完整回答（单次提问模式用，内部走与流式相同的网络抓取）。"""
    parts: list[str] = []
    answer = stream_answer(
        page, prompt, parts.append, org_uuid=org_uuid, timeout=timeout
    )
    save_debug(page, "claude-answer")
    if not answer:
        save_debug(page, "ask-empty-answer")
        raise RuntimeError("已发送问题但未抓取到回答内容。")
    return answer
