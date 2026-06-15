#!/usr/bin/env python3
"""CapSolver 对接：求解 Cloudflare Turnstile（项目唯一的过验证方式）。

严格按官方文档实现：https://docs.capsolver.com/en/guide/captcha/cloudflare_turnstile/

求解三步（全部对应官方 API）：
1. ``getBalance`` 校验 API Key / 余额；
2. ``createTask``（type=``AntiTurnstileTaskProxyLess``，必填 ``websiteURL`` / ``websiteKey``，
   可选 ``metadata.action`` / ``metadata.cdata``，分别对应 Turnstile 元素的
   ``data-action`` / ``data-cdata``）；
3. 轮询 ``getTaskResult`` 直到 ``status == "ready"``，从 ``solution.token`` 取回 token。

拿到 token 后写回页面（Cursor/WorkOS 用的是**隐形 Turnstile**，没有可见复选框）：
- 注入到所有 ``cf-turnstile-response`` 字段（含 Shadow DOM）；
- 触发提前 hook 到的 ``turnstile.render`` 回调，模拟真实校验通过。

仅在设置了 ``CAPSOLVER_API_KEY`` 时启用，所有步骤打印 ``[capsolver]`` 前缀日志。
"""

from __future__ import annotations

import os
import re
import time

import requests

CAPSOLVER_API_BASE = os.environ.get("CAPSOLVER_API_BASE", "https://api.capsolver.com").rstrip("/")

# 在页面脚本执行前 hook window.turnstile.render：
#  - 记录每个 widget 的 sitekey / action / cdata（隐形 Turnstile 唯一能拿到 sitekey 的途径）；
#  - 记录 callback，拿到 token 后像真实校验通过那样回调宿主页面。
TURNSTILE_HOOK_SCRIPT = """
(() => {
  if (window.__cfHookInstalled) return;
  window.__cfHookInstalled = true;
  window.__cfTurnstileCallbacks = window.__cfTurnstileCallbacks || [];
  window.__cfTurnstileParams = window.__cfTurnstileParams || [];
  const record = (params) => {
    try {
      if (!params) return;
      window.__cfTurnstileParams.push({
        sitekey: params.sitekey || params.siteKey || '',
        action: params.action || '',
        cdata: params.cData || params.cdata || '',
      });
      if (typeof params.callback === 'function') {
        window.__cfTurnstileCallbacks.push(params.callback);
      }
    } catch (e) {}
  };
  const patch = () => {
    if (!window.turnstile || window.__cfRenderPatched) return;
    if (typeof window.turnstile.render !== 'function') return;
    window.__cfRenderPatched = true;
    const origRender = window.turnstile.render;
    window.turnstile.render = function (container, params) {
      record(params);
      return origRender.apply(this, arguments);
    };
  };
  patch();
  const timer = setInterval(patch, 50);
  setTimeout(() => clearInterval(timer), 60000);
})();
"""

# 拿到 token 后注入页面：穿透 Shadow DOM 写回所有 response 字段，并逐个调用 hook 到的回调。
TURNSTILE_INJECT_SCRIPT = """
(token) => {
  const sel = 'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"],'
            + '#cf-turnstile-response,'
            + 'input[name="g-recaptcha-response"], textarea[name="g-recaptcha-response"]';
  let fields = 0;
  const visit = (root) => {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll(sel).forEach((el) => {
      el.value = token;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      fields += 1;
    });
    root.querySelectorAll('*').forEach((el) => { if (el.shadowRoot) visit(el.shadowRoot); });
  };
  visit(document);
  let callbacks = 0;
  (window.__cfTurnstileCallbacks || []).forEach((cb) => {
    try { cb(token); callbacks += 1; } catch (e) {}
  });
  return { fields, callbacks };
}
"""

# 读取 hook 捕获的 render 参数（隐形 Turnstile 的 sitekey 来源）。
READ_PARAMS_SCRIPT = "() => (window.__cfTurnstileParams || [])"

# 穿透 Shadow DOM 找 .cf-turnstile / [data-sitekey]（拿 sitekey + action + cdata）。
DETECT_DOM_SCRIPT = """
() => {
  const out = [];
  const visit = (root) => {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('.cf-turnstile, [data-sitekey]').forEach((el) => {
      out.push({
        sitekey: el.getAttribute('data-sitekey') || '',
        action: el.getAttribute('data-action') || '',
        cdata: el.getAttribute('data-cdata') || '',
      });
    });
    root.querySelectorAll('*').forEach((el) => { if (el.shadowRoot) visit(el.shadowRoot); });
  };
  visit(document);
  return out.find((x) => x.sitekey) || null;
}
"""

_SITEKEY_RE = re.compile(r"0x[0-9A-Za-z_-]{20,}")


class CapSolverError(Exception):
    """CapSolver 调用相关错误。"""


def _log(msg: str) -> None:
    print(msg, flush=True)


def is_enabled() -> bool:
    """是否配置了 CapSolver API Key。"""
    return bool(os.environ.get("CAPSOLVER_API_KEY", "").strip())


def _api_key() -> str:
    key = os.environ.get("CAPSOLVER_API_KEY", "").strip()
    if not key:
        raise CapSolverError("未配置 CAPSOLVER_API_KEY")
    return key


def _post(endpoint: str, payload: dict, timeout: int = 30) -> dict:
    resp = requests.post(f"{CAPSOLVER_API_BASE}/{endpoint}", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def log_account() -> None:
    """启动时调用 getBalance 校验 API Key 是否有效、余额是否充足。"""
    if not is_enabled():
        _log("[capsolver] ⚠️ 未配置 CAPSOLVER_API_KEY，CapSolver 已禁用（无法过 Turnstile）")
        return
    key = _api_key()
    masked = f"{key[:6]}...{key[-4:]}" if len(key) > 12 else "***"
    _log(f"[capsolver] API Key 已配置 ({masked})，base={CAPSOLVER_API_BASE}")
    try:
        data = _post("getBalance", {"clientKey": key})
    except Exception as exc:  # noqa: BLE001
        _log(f"[capsolver] ❌ getBalance 请求失败（API 对接异常）: {exc}")
        return
    if data.get("errorId"):
        _log(f"[capsolver] ❌ API Key 无效: {data.get('errorCode')} {data.get('errorDescription')}")
        return
    _log(
        f"[capsolver] ✅ API Key 有效，账户余额=${data.get('balance')} "
        f"packages={len(data.get('packages') or [])}"
    )


def install_hook(target) -> None:
    """在 BrowserContext 或 Page 上注入 turnstile.render hook（页面脚本执行前生效）。"""
    try:
        target.add_init_script(TURNSTILE_HOOK_SCRIPT)
        _log("[capsolver] 已注入 turnstile.render hook（捕获隐形 widget 的 sitekey/回调）")
    except Exception as exc:  # noqa: BLE001
        _log(f"[capsolver] ❌ 注入 hook 脚本失败: {exc}")


def _eval(frame, script, *args):
    try:
        return frame.evaluate(script, *args)
    except Exception:
        return None


def detect_turnstile(page) -> dict | None:
    """返回 {sitekey, url, action, cdata} 或 None，并打印检测过程。"""
    frames = [page.main_frame, *page.frames]
    _log(f"[capsolver] 检测 Turnstile：扫描 {len(frames)} 个 frame...")

    # 1) render hook 捕获的参数（隐形 Turnstile 首选，能拿到 action/cdata）
    for frame in frames:
        for params in _eval(frame, READ_PARAMS_SCRIPT) or []:
            if params and params.get("sitekey"):
                params["url"] = page.url
                _log(
                    f"[capsolver] ✅ 从 render hook 捕获 sitekey={params['sitekey']} "
                    f"action={params.get('action') or '无'} cdata={params.get('cdata') or '无'}"
                )
                return params

    # 2) DOM（含 Shadow DOM）上的 data-sitekey
    for frame in frames:
        params = _eval(frame, DETECT_DOM_SCRIPT)
        if params and params.get("sitekey"):
            params["url"] = page.url
            _log(
                f"[capsolver] ✅ 从 DOM 检测到 sitekey={params['sitekey']} "
                f"action={params.get('action') or '无'} cdata={params.get('cdata') or '无'}"
            )
            return params

    # 3) challenges.cloudflare.com iframe 的 URL 中解析 sitekey
    for frame in page.frames:
        url = frame.url or ""
        if "challenges.cloudflare.com" not in url:
            continue
        _log(f"[capsolver] 发现 Cloudflare challenge iframe: {url[:120]}")
        match = _SITEKEY_RE.search(url)
        if match:
            _log(f"[capsolver] ✅ 从 iframe URL 解析到 sitekey={match.group(0)}")
            return {"sitekey": match.group(0), "url": page.url, "action": "", "cdata": ""}

    # 4) 环境变量兜底（手动指定 Cursor 的 Turnstile sitekey）
    override = os.environ.get("CAPSOLVER_SITEKEY", "").strip()
    if override:
        _log(f"[capsolver] ✅ 使用 CAPSOLVER_SITEKEY 指定的 sitekey={override}")
        return {"sitekey": override, "url": page.url, "action": "", "cdata": ""}

    _log("[capsolver] ⚠️ 未在任何 frame 检测到 Turnstile sitekey")
    return None


def solve_turnstile(
    sitekey: str,
    website_url: str,
    *,
    action: str = "",
    cdata: str = "",
    poll_timeout: int = 120,
    poll_interval: float = 3.0,
) -> str:
    """createTask + 轮询 getTaskResult，返回 Turnstile token。

    参数与官方 AntiTurnstileTaskProxyLess 一一对应；轮询按文档 status（ready/processing/idle）处理。
    """
    key = _api_key()

    task: dict = {
        "type": "AntiTurnstileTaskProxyLess",
        "websiteURL": website_url,
        "websiteKey": sitekey,
    }
    metadata = {k: v for k, v in (("action", action), ("cdata", cdata)) if v}
    if metadata:
        task["metadata"] = metadata

    _log(
        f"[capsolver] → createTask sitekey={sitekey} url={website_url} "
        f"metadata={metadata or '无'}"
    )
    data = _post("createTask", {"clientKey": key, "task": task})
    if data.get("errorId"):
        raise CapSolverError(
            f"createTask 失败: {data.get('errorCode')} {data.get('errorDescription')}"
        )
    task_id = data.get("taskId")
    if not task_id:
        raise CapSolverError(f"createTask 未返回 taskId: {data}")
    _log(f"[capsolver] ← createTask 成功 taskId={task_id}，开始轮询结果...")

    start = time.time()
    deadline = start + poll_timeout
    polls = 0
    while time.time() < deadline:
        time.sleep(poll_interval)
        polls += 1
        payload = _post("getTaskResult", {"clientKey": key, "taskId": task_id})
        if payload.get("errorId"):
            raise CapSolverError(
                f"getTaskResult 失败: {payload.get('errorCode')} {payload.get('errorDescription')}"
            )

        status = payload.get("status")
        elapsed = time.time() - start
        if status == "ready":
            token = (payload.get("solution") or {}).get("token", "")
            if not token:
                raise CapSolverError(f"任务完成但未返回 token: {payload}")
            _log(
                f"[capsolver] ✅ 求解成功（{elapsed:.1f}s, {polls} 次轮询），"
                f"token 长度={len(token)} 预览={token[:24]}..."
            )
            return token
        _log(f"[capsolver] 轮询 #{polls} status={status}（已等待 {elapsed:.1f}s）")

    raise CapSolverError(f"轮询超时（{poll_timeout}s）未拿到结果")


def inject_token(page, token: str) -> bool:
    """把 token 写回页面（穿透 Shadow DOM）并触发回调，返回是否成功应用。"""
    total_fields = 0
    total_cbs = 0
    for frame in [page.main_frame, *page.frames]:
        res = _eval(frame, TURNSTILE_INJECT_SCRIPT, token)
        if res:
            total_fields += res.get("fields", 0)
            total_cbs += res.get("callbacks", 0)
    if total_fields or total_cbs:
        _log(f"[capsolver] token 已注入：写入 {total_fields} 个字段，触发 {total_cbs} 个回调")
        return True
    _log("[capsolver] ❌ 未找到可注入的 response 字段/回调（token 无处可用）")
    return False


def solve_when_present(page, *, label: str = "", wait_s: int = 8) -> bool:
    """在 wait_s 秒内轮询等待 Turnstile 出现（隐形 widget 渲染有延迟），出现即求解并注入。

    返回是否成功注入 token。未检测到 Turnstile 返回 False（不算错误）。
    """
    prefix = f"{label} " if label else ""
    if not is_enabled():
        _log(f"[capsolver] {prefix}⚠️ 未配置 CAPSOLVER_API_KEY，跳过求解")
        return False

    deadline = time.time() + wait_s
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        params = detect_turnstile(page)
        if params:
            _log(f"[capsolver] {prefix}检测到 Turnstile，开始求解（第 {attempt} 次检测命中）")
            try:
                token = solve_turnstile(
                    params["sitekey"],
                    params["url"],
                    action=params.get("action", ""),
                    cdata=params.get("cdata", ""),
                )
            except (CapSolverError, requests.RequestException) as exc:
                _log(f"[capsolver] {prefix}❌ 求解失败: {exc}")
                return False
            return inject_token(page, token)
        try:
            page.wait_for_timeout(1000)
        except Exception:
            time.sleep(1)

    _log(f"[capsolver] {prefix}{wait_s}s 内未检测到 Turnstile，跳过")
    return False
