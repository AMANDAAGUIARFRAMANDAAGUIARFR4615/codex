"""Cloudflare Turnstile 状态检测辅助。

历史上这里包含 cliclick / 真人鼠标点击复选框的「过验证」方案，但实测无效，已全部移除。
现在 Turnstile 一律由 CapSolver 求解（见 capsolver_solver.py），本模块只保留两个轻量的
状态判断函数：判断页面是否处于 Cloudflare 验证、读取已写入的 Turnstile token。
"""

from __future__ import annotations

from io_utils import setup_utf8_stdio

setup_utf8_stdio()


def is_cloudflare_challenge(title: str, body: str) -> bool:
    text = f"{title}\n{body}".lower()
    markers = (
        "just a moment",
        "checking your browser",
        "verify you are human",
        "attention required",
        "cloudflare",
        "cf-turnstile",
        "确认您是真人",
        # 点击 Email sign-in code 后 Cursor/WorkOS 页面主 DOM 上的提示文案
        "before continuing",
        "sure you are human",
    )
    return any(marker in text for marker in markers)


def read_turnstile_token(page) -> str:
    """读取 Turnstile 校验通过后写入的 cf-turnstile-response token（非空即已通过）。"""
    js = (
        "() => {"
        " const names=['cf-turnstile-response','g-recaptcha-response'];"
        " for (const n of names){"
        "  const el=document.querySelector(`[name=\"${n}\"]`)"
        "    || document.querySelector(`#${n}`);"
        "  if (el && el.value) return el.value;"
        " }"
        " return ''; }"
    )
    try:
        return page.evaluate(js) or ""
    except Exception:
        return ""
