#!/usr/bin/env python3
"""
过 claude.ai Cloudflare 验证，用 Cookie-Editor 导入 cookie.json，
重新加载页面并截图供核验。

过 Cloudflare Turnstile 的方式只有一种：CapSolver（见 capsolver_solver.py）。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

try:
    from patchright.sync_api import Browser, BrowserContext, Page, sync_playwright

    _USING_PATCHRIGHT = True
except ImportError:
    from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

    _USING_PATCHRIGHT = False

import capsolver_solver
from cookie_import import has_claude_session, import_cookies, log
from debug_utils import save_debug
from io_utils import setup_utf8_stdio

setup_utf8_stdio()

CLAUDE_URL = "https://claude.ai/"

_CLOUDFLARE_MARKERS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "attention required",
    "cloudflare",
    "cf-turnstile",
    "确认您是真人",
    "before continuing",
    "sure you are human",
)

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


def is_cloudflare_page(title: str, body: str) -> bool:
    text = f"{title}\n{body}".lower()
    return any(marker in text for marker in _CLOUDFLARE_MARKERS)


def get_extension_dir() -> Path | None:
    if os.environ.get("LOAD_COOKIE_EXTENSION", "true").lower() != "true":
        return None

    ext = os.environ.get("COOKIE_EDITOR_DIR", "").strip()
    if not ext:
        return None
    path = Path(ext)
    if path.exists() and (path / "manifest.json").exists():
        return path
    return None


def _extra_browser_args() -> list[str]:
    win_size = os.environ.get("BROWSER_WINDOW_SIZE", "1280,1024")
    args: list[str] = [
        "--window-position=0,0",
        f"--window-size={win_size}",
    ]
    if sys.platform == "linux":
        args.extend(["--no-sandbox", "--disable-dev-shm-usage"])
    extension_dir = get_extension_dir()
    if extension_dir:
        ext_path = str(extension_dir.resolve())
        log(f"[browser] 加载 Cookie-Editor 扩展: {ext_path}")
        args.extend(
            [
                f"--disable-extensions-except={ext_path}",
                f"--load-extension={ext_path}",
            ]
        )
    return args


def resolve_browser_channel() -> str | None:
    """加载未打包扩展必须用 Chromium；系统 Google Chrome 会忽略 --load-extension。"""
    configured = os.environ.get("PLAYWRIGHT_CHANNEL", "").strip().lower()
    if get_extension_dir() is not None:
        if configured and configured not in ("chromium", ""):
            log(
                "[browser] 已加载 Cookie-Editor，强制使用 patchright Chromium "
                f"(忽略 PLAYWRIGHT_CHANNEL={configured})"
            )
        return None
    if configured:
        return None if configured == "chromium" else configured
    return "chrome" if sys.platform in ("darwin", "win32") else None


def launch_browser(playwright) -> tuple[Browser | None, BrowserContext]:
    channel = resolve_browser_channel()
    use_headless = os.environ.get("PLAYWRIGHT_HEADLESS", "false").lower() == "true"
    extra_args = _extra_browser_args()
    channel_label = channel or "chromium"

    log(
        f"[browser] 启动浏览器 (platform={sys.platform}, channel={channel_label}, "
        f"headless={use_headless}, patchright={_USING_PATCHRIGHT})..."
    )

    if _USING_PATCHRIGHT:
        user_data_dir = os.environ.get("USER_DATA_DIR", "").strip() or tempfile.mkdtemp(
            prefix="claude-cookie-profile-"
        )
        ctx_kwargs: dict = {
            "user_data_dir": user_data_dir,
            "headless": use_headless,
            "timeout": 60_000,
            "no_viewport": True,
            "locale": "en-US",
            "timezone_id": "America/Los_Angeles",
        }
        if channel and channel != "chromium":
            ctx_kwargs["channel"] = channel
        if extra_args:
            ctx_kwargs["args"] = extra_args

        log(f"[browser] patchright 持久化上下文: {user_data_dir}")
        context = playwright.chromium.launch_persistent_context(**ctx_kwargs)
        capsolver_solver.install_hook(context)
        log("[browser] BrowserContext 已创建")
        return None, context

    launch_kwargs: dict = {
        "headless": use_headless,
        "timeout": 60_000,
        "args": ["--disable-blink-features=AutomationControlled", *extra_args],
    }
    if channel and channel != "chromium":
        launch_kwargs["channel"] = channel

    browser = playwright.chromium.launch(**launch_kwargs)
    log("[browser] 浏览器进程已启动")

    context = browser.new_context(
        viewport={"width": 1366, "height": 900},
        locale="en-US",
        timezone_id="America/Los_Angeles",
    )
    context.add_init_script(STEALTH_SCRIPT)
    capsolver_solver.install_hook(context)
    log("[browser] BrowserContext 已创建")
    return browser, context


def solve_turnstile(page: Page, label: str = "", wait_s: int = 8) -> bool:
    return capsolver_solver.solve_when_present(page, label=label, wait_s=wait_s)


def wait_for_claude_ready(page: Page, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    last_log = 0.0

    while time.time() < deadline:
        now = time.time()
        if now - last_log >= 10:
            log(f"[claude] 等待页面就绪... url={page.url}")
            last_log = now

        title = (page.title() or "").lower()
        body = ""
        try:
            body = page.locator("body").inner_text(timeout=3000).lower()
        except Exception:
            pass

        if is_cloudflare_page(title, body):
            log("[claude] 检测到 Cloudflare 验证页，用 CapSolver 求解...")
            solve_turnstile(page, "页面加载", wait_s=12)
            page.wait_for_timeout(2000)
            continue

        if "claude.ai" in page.url and not is_cloudflare_page(title, body):
            log(f"[claude] 已通过 Cloudflare: {page.url}")
            return

        page.wait_for_timeout(1500)

    save_debug(page, "cloudflare-timeout")
    raise TimeoutError("claude.ai 长时间未通过 Cloudflare 验证。")


def open_claude(page: Page) -> None:
    log(f"[claude] 打开 {CLAUDE_URL}")
    page.goto(CLAUDE_URL, wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(2000)
    wait_for_claude_ready(page)


def reload_and_verify(context: BrowserContext, page: Page) -> None:
    log("[claude] 导入完成，重新加载页面...")
    page.reload(wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(3000)
    wait_for_claude_ready(page, timeout=60)

    if not has_claude_session(context):
        save_debug(page, "session-missing-after-import")
        raise RuntimeError("导入后未检测到 sessionKey cookie，登录可能失败。")

    log(f"[claude] 检测到 sessionKey，当前 URL: {page.url}")


def save_result_screenshot(page: Page, label: str = "claude-after-import") -> Path:
    save_debug(page, label)
    screenshot = Path("debug") / f"{label}.png"
    log(f"[done] 结果截图: {screenshot.resolve()}")
    return screenshot


def _install_network_logger(page: Page) -> None:
    def _is_cf(url: str) -> bool:
        return "challenges.cloudflare.com" in url or "turnstile" in url

    def on_response(resp) -> None:
        try:
            if _is_cf(resp.url):
                log(f"[net] {resp.status} {resp.url[:110]}")
        except Exception:
            pass

    def on_failed(request) -> None:
        try:
            if _is_cf(request.url):
                log(f"[net] FAILED {request.failure} {request.url[:110]}")
        except Exception:
            pass

    page.on("response", on_response)
    page.on("requestfailed", on_failed)


def _install_cancel_debug_handler(page: Page) -> None:
    def on_cancel(signum: int, _frame) -> None:
        label = "cancelled" if signum == signal.SIGTERM else "interrupted"
        log(f"[debug] 收到终止信号 ({signum})，保存当前页面...")
        save_debug(page, label)
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, on_cancel)
    signal.signal(signal.SIGINT, on_cancel)


def resolve_cookie_file(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        repo_root = Path(__file__).resolve().parent.parent
        candidate = repo_root / path
        if candidate.exists():
            path = candidate
    if not path.exists():
        raise FileNotFoundError(f"未找到 cookie 文件: {path}")
    return path.resolve()


def run(cookie_file: Path) -> Path:
    log("[start] Claude Cookie 导入脚本启动")
    log(f"[start] Cookie 文件: {cookie_file}")

    if get_extension_dir() is None:
        raise RuntimeError(
            "未加载 Cookie-Editor 扩展。请设置 LOAD_COOKIE_EXTENSION=true 和 COOKIE_EDITOR_DIR。"
        )

    capsolver_solver.log_account()

    with sync_playwright() as playwright:
        log("[browser] Playwright 已初始化")
        browser, context = launch_browser(playwright)
        page = context.pages[0] if context.pages else context.new_page()
        _install_network_logger(page)
        page.set_default_timeout(5000)
        log("[browser] 标签页已就绪（默认超时 5s）")
        _install_cancel_debug_handler(page)

        try:
            open_claude(page)
            import_cookies(context, page, cookie_file, use_extension=True)
            reload_and_verify(context, page)
            return save_result_screenshot(page)
        except Exception:
            save_debug(page, "error")
            raise
        finally:
            context.close()
            if browser is not None:
                browser.close()
            log("[browser] 浏览器已关闭")


def main() -> None:
    log("[boot] Python 进程已启动")
    parser = argparse.ArgumentParser(description="过 claude.ai 验证并导入 Cookie")
    parser.add_argument(
        "cookie_file",
        nargs="?",
        default=os.environ.get("COOKIE_INPUT_FILE", "cookie.json"),
        help="Cookie-Editor JSON 文件路径（默认 cookie.json）",
    )
    args = parser.parse_args()

    try:
        cookie_path = resolve_cookie_file(args.cookie_file)
        screenshot = run(cookie_path)
        print(f"[done] 导入完成，截图已保存: {screenshot}")
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
