#!/usr/bin/env python3
"""通过 CapSolver API 求解 Cloudflare Turnstile。

工作流程：
1. 从页面提取 Turnstile 的 sitekey（data-sitekey 或 challenges.cloudflare.com iframe 的 k 参数）；
2. 调 CapSolver `createTask`（AntiTurnstileTaskProxyLess）+ 轮询 `getTaskResult` 拿 token；
3. 把 token 写回页面的 cf-turnstile-response 字段，并触发被 hook 捕获的 turnstile 回调，
   让宿主页面继续后续登录流程。

仅在设置了环境变量 `CAPSOLVER_API_KEY` 时启用；未配置时调用方应回退到原有点击方案。
文档参考: https://docs.capsolver.com/
"""

from __future__ import annotations

import os
import re
import time

import requests

CAPSOLVER_API_BASE = os.environ.get("CAPSOLVER_API_BASE", "https://api.capsolver.com").rstrip("/")

# 在宿主页面提前 hook window.turnstile.render，捕获每个 widget 的 callback，
# 以便拿到 token 后能像真实校验通过那样回调宿主页面。必须在页面脚本执行前注入。
TURNSTILE_HOOK_SCRIPT = """
(() => {
  if (window.__cfHookInstalled) return;
  window.__cfHookInstalled = true;
  window.__cfTurnstileCallbacks = window.__cfTurnstileCallbacks || [];
  const patch = () => {
    if (!window.turnstile || window.__cfRenderPatched) return;
    window.__cfRenderPatched = true;
    const origRender = window.turnstile.render;
    if (typeof origRender !== 'function') return;
    window.turnstile.render = function (container, params) {
      try {
        if (params && typeof params.callback === 'function') {
          window.__cfTurnstileCallbacks.push(params.callback);
        }
      } catch (e) {}
      return origRender.apply(this, arguments);
    };
  };
  patch();
  const timer = setInterval(patch, 50);
  setTimeout(() => clearInterval(timer), 30000);
})();
"""

# 拿到 token 后注入页面：写回所有 response 字段并逐个调用被 hook 捕获的回调。
TURNSTILE_INJECT_SCRIPT = """
(token) => {
  let applied = false;
  const fields = document.querySelectorAll(
    'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"],' +
    '#cf-turnstile-response,' +
    'input[name="g-recaptcha-response"], textarea[name="g-recaptcha-response"]'
  );
  fields.forEach((el) => {
    el.value = token;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    applied = true;
  });
  try {
    const cbs = window.__cfTurnstileCallbacks || [];
    cbs.forEach((cb) => {
      try { cb(token); applied = true; } catch (e) {}
    });
  } catch (e) {}
  return applied;
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


def install_hook(target) -> None:
    """在 BrowserContext 或 Page 上注入 turnstile.render hook（页面脚本执行前生效）。"""
    try:
        target.add_init_script(TURNSTILE_HOOK_SCRIPT)
    except Exception as exc:  # noqa: BLE001
        _log(f"[capsolver] 注入 hook 脚本失败: {exc}")


def _detect_from_dom(frame) -> dict | None:
    js = """
    () => {
      const el = document.querySelector('.cf-turnstile, [data-sitekey]');
      if (!el) return null;
      return {
        sitekey: el.getAttribute('data-sitekey') || '',
        action: el.getAttribute('data-action') || '',
        cdata: el.getAttribute('data-cdata') || '',
      };
    }
    """
    try:
        res = frame.evaluate(js)
    except Exception:
        return None
    if res and res.get("sitekey"):
        return res
    return None


def detect_turnstile(page) -> dict | None:
    """返回 {sitekey, url, action, cdata} 或 None。"""
    frames = [page.main_frame, *page.frames]

    for frame in frames:
        params = _detect_from_dom(frame)
        if params:
            params["url"] = page.url
            _log(f"[capsolver] 从 DOM 检测到 sitekey: {params['sitekey']}")
            return params

    # 回退：从 challenges.cloudflare.com iframe 的 URL 中解析 sitekey（0x 开头）
    for frame in page.frames:
        url = frame.url or ""
        if "challenges.cloudflare.com" not in url:
            continue
        match = _SITEKEY_RE.search(url)
        if match:
            sitekey = match.group(0)
            _log(f"[capsolver] 从 iframe URL 解析到 sitekey: {sitekey}")
            return {"sitekey": sitekey, "url": page.url, "action": "", "cdata": ""}

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
    """调用 CapSolver 求解 Turnstile，返回 token。"""
    key = _api_key()

    task: dict = {
        "type": "AntiTurnstileTaskProxyLess",
        "websiteURL": website_url,
        "websiteKey": sitekey,
    }
    metadata: dict = {}
    if action:
        metadata["action"] = action
    if cdata:
        metadata["cdata"] = cdata
    if metadata:
        task["metadata"] = metadata

    _log(f"[capsolver] 提交任务: sitekey={sitekey} url={website_url} metadata={metadata or '无'}")
    resp = requests.post(
        f"{CAPSOLVER_API_BASE}/createTask",
        json={"clientKey": key, "task": task},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errorId"):
        raise CapSolverError(
            f"createTask 失败: {data.get('errorCode')} {data.get('errorDescription')}"
        )

    task_id = data.get("taskId")
    if not task_id:
        raise CapSolverError(f"createTask 未返回 taskId: {data}")

    deadline = time.time() + poll_timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        result = requests.post(
            f"{CAPSOLVER_API_BASE}/getTaskResult",
            json={"clientKey": key, "taskId": task_id},
            timeout=30,
        )
        result.raise_for_status()
        payload = result.json()
        if payload.get("errorId"):
            raise CapSolverError(
                f"getTaskResult 失败: {payload.get('errorCode')} {payload.get('errorDescription')}"
            )

        status = payload.get("status")
        if status == "ready":
            token = (payload.get("solution") or {}).get("token", "")
            if not token:
                raise CapSolverError(f"任务完成但未返回 token: {payload}")
            _log(f"[capsolver] 求解成功，token 长度={len(token)}")
            return token
        _log("[capsolver] 任务处理中，等待...")

    raise CapSolverError(f"轮询超时（{poll_timeout}s）未拿到结果")


def inject_token(page, token: str) -> bool:
    """把 token 写回页面并触发回调，返回是否成功应用。"""
    applied = False
    for frame in [page.main_frame, *page.frames]:
        try:
            if frame.evaluate(TURNSTILE_INJECT_SCRIPT, token):
                applied = True
        except Exception:
            continue
    if applied:
        _log("[capsolver] token 已注入页面")
    else:
        _log("[capsolver] 未找到可注入的 response 字段/回调")
    return applied


def solve_on_page(page, *, poll_timeout: int = 120) -> bool:
    """检测页面 Turnstile 并用 CapSolver 求解、注入。整体成功返回 True。"""
    if not is_enabled():
        return False

    params = detect_turnstile(page)
    if not params:
        _log("[capsolver] 页面未检测到 Turnstile sitekey")
        return False

    try:
        token = solve_turnstile(
            params["sitekey"],
            params["url"],
            action=params.get("action", ""),
            cdata=params.get("cdata", ""),
            poll_timeout=poll_timeout,
        )
    except (CapSolverError, requests.RequestException) as exc:
        _log(f"[capsolver] 求解失败: {exc}")
        return False

    return inject_token(page, token)
