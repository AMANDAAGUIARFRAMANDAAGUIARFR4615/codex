#!/usr/bin/env python3
"""通过 CapSolver API 求解 Cloudflare Turnstile（项目唯一的过验证方式）。

工作流程：
1. 在页面脚本执行前 hook ``window.turnstile.render``，捕获每个 widget 的
   ``sitekey / action / cData`` 以及 ``callback``。Cursor/WorkOS 用的是**隐形 Turnstile**
   （没有可见复选框，也没有 data-sitekey 节点），只能从 render 参数里拿到 sitekey；
2. 调 CapSolver ``createTask``（AntiTurnstileTaskProxyLess）+ 轮询 ``getTaskResult`` 拿 token；
3. 把 token 写回页面的 cf-turnstile-response 字段，并触发被 hook 捕获的 turnstile 回调，
   让宿主页面继续后续登录流程。

仅在设置了环境变量 ``CAPSOLVER_API_KEY`` 时启用。所有步骤都打印 ``[capsolver]`` 前缀日志，
方便在 CI 日志里确认对接是否正常。
文档参考: https://docs.capsolver.com/
"""

from __future__ import annotations

import os
import re
import time

import requests

CAPSOLVER_API_BASE = os.environ.get("CAPSOLVER_API_BASE", "https://api.capsolver.com").rstrip("/")

# 在宿主页面提前 hook window.turnstile.render：
#  - 记录每个 widget 的 sitekey/action/cData（隐形 Turnstile 唯一能拿到 sitekey 的途径）；
#  - 记录 callback，拿到 token 后像真实校验通过那样回调宿主页面。
# 必须在页面脚本执行前注入（add_init_script）。
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
    window.__cfRenderPatched = true;
    const origRender = window.turnstile.render;
    if (typeof origRender !== 'function') return;
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

# 拿到 token 后注入页面：写回所有 response 字段并逐个调用被 hook 捕获的回调。
TURNSTILE_INJECT_SCRIPT = """
(token) => {
  let fieldCount = 0;
  let cbCount = 0;
  const fields = document.querySelectorAll(
    'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"],' +
    '#cf-turnstile-response,' +
    'input[name="g-recaptcha-response"], textarea[name="g-recaptcha-response"]'
  );
  fields.forEach((el) => {
    el.value = token;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    fieldCount += 1;
  });
  try {
    const cbs = window.__cfTurnstileCallbacks || [];
    cbs.forEach((cb) => {
      try { cb(token); cbCount += 1; } catch (e) {}
    });
  } catch (e) {}
  return { fields: fieldCount, callbacks: cbCount };
}
"""

# 读取 hook 捕获到的 render 参数（隐形 Turnstile 的 sitekey 来源）。
READ_PARAMS_SCRIPT = "() => (window.__cfTurnstileParams || [])"

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


def log_account() -> None:
    """启动时调用 getBalance 验证 API Key 是否有效、余额是否充足。

    这是确认 CapSolver “对接好没有” 的最快方式：
    Key 无效会直接报错，余额为 0 也会在日志里看到。
    """
    if not is_enabled():
        _log("[capsolver] ⚠️ 未配置 CAPSOLVER_API_KEY，CapSolver 已禁用（无法过 Turnstile）")
        return
    key = _api_key()
    masked = f"{key[:6]}...{key[-4:]}" if len(key) > 12 else "***"
    _log(f"[capsolver] API Key 已配置 ({masked})，base={CAPSOLVER_API_BASE}")
    try:
        resp = requests.post(
            f"{CAPSOLVER_API_BASE}/getBalance",
            json={"clientKey": key},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        _log(f"[capsolver] ❌ getBalance 请求失败（API 对接异常）: {exc}")
        return
    if data.get("errorId"):
        _log(
            f"[capsolver] ❌ API Key 无效: {data.get('errorCode')} "
            f"{data.get('errorDescription')}"
        )
        return
    balance = data.get("balance")
    packages = data.get("packages") or []
    _log(f"[capsolver] ✅ API Key 有效，账户余额=${balance} packages={len(packages)}")


def install_hook(target) -> None:
    """在 BrowserContext 或 Page 上注入 turnstile.render hook（页面脚本执行前生效）。"""
    try:
        target.add_init_script(TURNSTILE_HOOK_SCRIPT)
        _log("[capsolver] 已注入 turnstile.render hook（捕获隐形 widget 的 sitekey/回调）")
    except Exception as exc:  # noqa: BLE001
        _log(f"[capsolver] ❌ 注入 hook 脚本失败: {exc}")


def _read_hook_params(frame) -> dict | None:
    """从 render hook 捕获的参数里取 sitekey（隐形 Turnstile）。"""
    try:
        params_list = frame.evaluate(READ_PARAMS_SCRIPT) or []
    except Exception:
        return None
    for params in params_list:
        if params and params.get("sitekey"):
            return params
    return None


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
    """返回 {sitekey, url, action, cdata} 或 None，并打印检测过程。"""
    frames = [page.main_frame, *page.frames]
    _log(f"[capsolver] 检测 Turnstile：扫描 {len(frames)} 个 frame...")

    # 1) render hook 捕获的参数（隐形 Turnstile 首选）
    for frame in frames:
        params = _read_hook_params(frame)
        if params:
            params["url"] = page.url
            _log(
                f"[capsolver] ✅ 从 render hook 捕获 sitekey={params['sitekey']} "
                f"action={params.get('action') or '无'} cdata={params.get('cdata') or '无'}"
            )
            return params

    # 2) DOM 上的 data-sitekey
    for frame in frames:
        params = _detect_from_dom(frame)
        if params:
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
            sitekey = match.group(0)
            _log(f"[capsolver] ✅ 从 iframe URL 解析到 sitekey={sitekey}")
            return {"sitekey": sitekey, "url": page.url, "action": "", "cdata": ""}

    # 4) 环境变量兜底（手动指定 Cursor 的 Turnstile sitekey）
    override = os.environ.get("CAPSOLVER_SITEKEY", "").strip()
    if override:
        _log(f"[capsolver] ✅ 使用 CAPSOLVER_SITEKEY 环境变量指定的 sitekey={override}")
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
    """调用 CapSolver 求解 Turnstile，返回 token。全过程打印日志。"""
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

    _log(
        f"[capsolver] → createTask sitekey={sitekey} url={website_url} "
        f"metadata={metadata or '无'}"
    )
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
    _log(f"[capsolver] ← createTask 成功 taskId={task_id}，开始轮询结果...")

    deadline = time.time() + poll_timeout
    start = time.time()
    polls = 0
    while time.time() < deadline:
        time.sleep(poll_interval)
        polls += 1
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
    """把 token 写回页面并触发回调，返回是否成功应用。"""
    total_fields = 0
    total_cbs = 0
    for frame in [page.main_frame, *page.frames]:
        try:
            res = frame.evaluate(TURNSTILE_INJECT_SCRIPT, token)
        except Exception:
            continue
        if res:
            total_fields += res.get("fields", 0)
            total_cbs += res.get("callbacks", 0)
    applied = (total_fields + total_cbs) > 0
    if applied:
        _log(f"[capsolver] token 已注入：写入 {total_fields} 个字段，触发 {total_cbs} 个回调")
    else:
        _log("[capsolver] ❌ 未找到可注入的 response 字段/回调（token 无处可用）")
    return applied


def solve_on_page(page, *, label: str = "", poll_timeout: int = 120) -> bool:
    """检测页面 Turnstile 并用 CapSolver 求解、注入。整体成功返回 True。"""
    prefix = f"{label}" if label else ""
    if not is_enabled():
        _log(f"[capsolver] {prefix}⚠️ 未配置 CAPSOLVER_API_KEY，跳过求解")
        return False

    params = detect_turnstile(page)
    if not params:
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
        _log(f"[capsolver] {prefix}❌ 求解失败: {exc}")
        return False

    return inject_token(page, token)


def solve_when_present(page, *, label: str = "", wait_s: int = 8) -> bool:
    """在 wait_s 秒内轮询等待 Turnstile 出现（隐形 widget 渲染有延迟），出现即求解。

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
