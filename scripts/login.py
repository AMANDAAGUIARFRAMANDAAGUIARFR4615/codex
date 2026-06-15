#!/usr/bin/env python3
"""
使用邮箱验证码登录 cursor.com，并以 Cookie-Editor JSON 格式导出 cookie。

输入格式: email----password
示例: SapphiraCaelum5932@outlook.com----rq757721
"""

from __future__ import annotations

import argparse
import os
import random
import re
import signal
import sys
import tempfile
import time
from pathlib import Path

# patchright 是经过反检测修补的 Playwright（修复 CDP Runtime.enable 泄露等），
# 用于通过 Cloudflare Turnstile；未安装时回退到原版 playwright。
try:
    from patchright.sync_api import (
        Browser,
        BrowserContext,
        Frame,
        Locator,
        Page,
        sync_playwright,
    )

    _USING_PATCHRIGHT = True
except ImportError:
    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Frame,
        Locator,
        Page,
        sync_playwright,
    )

    _USING_PATCHRIGHT = False

from cookie_export import print_cookie_editor_export
from debug_utils import save_debug
from io_utils import setup_utf8_stdio
from mailbox import MailboxClient
from turnstile_solver import (
    cliclick_at,
    click_turnstile_checkbox,
    find_visible_turnstile,
    focus_chrome,
    is_cloudflare_challenge,
    viewport_to_screen,
)

setup_utf8_stdio()

LOGIN_URLS = [
    "https://www.cursor.com/api/auth/login",
    "https://authenticator.cursor.sh/",
    "https://authenticate.cursor.sh/",
]

CURSOR_URL = "https://www.cursor.com/"
SETTINGS_URL = "https://www.cursor.com/settings"

EMAIL_SELECTORS = [
    'input[name="email"]',
    'input[type="email"]',
    'input[autocomplete="email"]',
    'input[placeholder*="email" i]',
    'input[placeholder*="邮箱" i]',
]

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_credentials(raw: str) -> tuple[str, str]:
    if "----" not in raw:
        raise ValueError("凭证格式应为: 邮箱----邮箱密码")
    email, password = raw.split("----", 1)
    email = email.strip()
    password = password.strip()
    if not email or not password:
        raise ValueError("邮箱或密码不能为空")
    if "@" not in email:
        raise ValueError(f"邮箱格式无效: {email}")
    return email, password


def get_extension_dir() -> Path | None:
    if os.environ.get("LOAD_COOKIE_EXTENSION", "false").lower() != "true":
        return None

    ext = os.environ.get("COOKIE_EDITOR_DIR", "").strip()
    if not ext:
        return None
    path = Path(ext)
    if path.exists() and (path / "manifest.json").exists():
        return path
    return None


def _extra_browser_args() -> list[str]:
    args: list[str] = []
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


def launch_browser(playwright) -> tuple[Browser | None, BrowserContext]:
    channel = os.environ.get(
        "PLAYWRIGHT_CHANNEL",
        "chrome" if sys.platform in ("darwin", "win32") else "chromium",
    )
    use_headless = os.environ.get("PLAYWRIGHT_HEADLESS", "false").lower() == "true"
    extra_args = _extra_browser_args()

    log(
        f"[browser] 启动浏览器 (platform={sys.platform}, channel={channel}, "
        f"headless={use_headless}, patchright={_USING_PATCHRIGHT})..."
    )

    # patchright 的最强反检测模式：持久化上下文 + 真实 Chrome 通道 + 不注入 stealth 脚本、
    # 不添加自动化相关启动参数（这些都会被 Cloudflare 指纹识别）。
    if _USING_PATCHRIGHT:
        user_data_dir = os.environ.get("USER_DATA_DIR", "").strip() or tempfile.mkdtemp(
            prefix="cursorcookie-profile-"
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
    log("[browser] BrowserContext 已创建")
    return browser, context


def iter_frames(page: Page) -> list[Frame]:
    return [page.main_frame, *page.frames]


def find_visible_locator(page: Page, selectors: list[str], timeout_ms: int = 5000) -> Locator | None:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for frame in iter_frames(page):
            for selector in selectors:
                locator = frame.locator(selector)
                if locator.count() == 0:
                    continue
                try:
                    if locator.first.is_visible():
                        return locator.first
                except Exception:
                    continue
        page.wait_for_timeout(500)
    return None


def wait_for_page_ready(page: Page, timeout: int = 90) -> None:
    deadline = time.time() + timeout
    last_log = 0.0

    while time.time() < deadline:
        now = time.time()
        if now - last_log >= 10:
            log(f"[login] 等待页面就绪... url={page.url}")
            last_log = now

        title = (page.title() or "").lower()
        body = ""
        try:
            body = page.locator("body").inner_text(timeout=3000).lower()
        except Exception:
            pass

        blocked_markers = (
            "just a moment",
            "checking your browser",
            "verify you are human",
            "attention required",
            "cloudflare",
        )
        if any(marker in title or marker in body for marker in blocked_markers):
            log("[login] 检测到 Cloudflare 验证，尝试自动处理...")
            try_click_visible_turnstile(page, "页面加载 ")
            page.wait_for_timeout(2000)
            continue

        if find_visible_locator(page, EMAIL_SELECTORS, timeout_ms=1000):
            return
        if page.locator('[data-index="0"]').count() > 0:
            return

        page.wait_for_timeout(1500)

    raise TimeoutError("页面长时间未加载出登录表单，可能被 Cloudflare 拦截。")


def open_login_page(page: Page) -> None:
    last_error: Exception | None = None

    for url in LOGIN_URLS:
        try:
            print(f"[login] 打开登录入口: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(3000)

            if "cursor.com" in page.url and "api/auth/login" not in page.url:
                page.wait_for_timeout(2000)

            wait_for_page_ready(page)
            print(f"[login] 页面就绪: {page.url}")
            return
        except Exception as exc:
            last_error = exc
            print(f"[login] 入口失败 {url}: {exc}")
            save_debug(page, f"login-failed-{LOGIN_URLS.index(url)}")

    save_debug(page, "login-failed-final")
    raise RuntimeError(f"无法打开 Cursor 登录页: {last_error}")


def is_password_url(page: Page) -> bool:
    """仅 URL 含 /password 才算误入密码流程（首页同时有邮箱+密码框不算）。"""
    return "/password" in page.url


def fill_email_field(page: Page, email: str) -> bool:
    email_input = find_visible_locator(page, EMAIL_SELECTORS, timeout_ms=10000)
    if email_input is None:
        return False
    email_input.click()
    email_input.fill("")
    email_input.fill(email)
    page.wait_for_timeout(500)
    return True


def _locators_for_label(frame: Frame, label: str) -> list[Locator]:
    return [
        frame.get_by_role("button", name=label),
        frame.get_by_role("link", name=label),
        frame.locator(f'button:has-text("{label}")'),
        frame.locator(f'a:has-text("{label}")'),
    ]


def _click_strategies(page: Page, locator: Locator):
    strategies = [
        ("click", lambda: locator.click(timeout=8000)),
        ("mouse", lambda: _mouse_click_locator(page, locator)),
        ("js", lambda: locator.evaluate("el => el.click()", timeout=4000)),
    ]
    if sys.platform == "darwin":
        strategies.append(("cliclick", lambda: _cliclick_locator(page, locator)))
    return strategies


def human_click_locator(page: Page, locator: Locator, *, label: str = "") -> bool:
    """尽量模拟真实用户点击，避免 force click 导致 React 按钮无响应。"""
    try:
        locator.wait_for(state="visible", timeout=8000)
        locator.scroll_into_view_if_needed()
        page.wait_for_timeout(random.randint(200, 500))
    except Exception:
        return False

    for name, action in _click_strategies(page, locator):
        try:
            action()
            log(f"[login] 已点击 {label or '元素'} ({name})")
            page.wait_for_timeout(random.randint(800, 1500))
            return True
        except Exception as exc:
            log(f"[login] 点击失败 ({name}): {exc}")
    return False


def click_email_code_button(page: Page) -> bool:
    label = "Email sign-in code"
    for frame in iter_frames(page):
        for loc in _locators_for_label(frame, label):
            try:
                if loc.count() == 0 or not loc.first.is_visible():
                    continue
                target = loc.first
                target.wait_for(state="visible", timeout=8000)
                target.scroll_into_view_if_needed()
                page.wait_for_timeout(random.randint(200, 500))

                for name, action in _click_strategies(page, target):
                    try:
                        action()
                        log(f"[login] 已点击 {label} ({name})")
                        page.wait_for_timeout(random.randint(800, 1500))
                        # 只要有一种方式点击成功就立即返回：点击后按钮会变成
                        # spinner，accessible name 不再是 "Email sign-in code"，
                        # 继续尝试其它策略会对已消失的按钮 bounding_box/evaluate
                        # 等待 30s 而白白超时。后续是否进入验证码页交给调用方等待。
                        return True
                    except Exception as exc:
                        log(f"[login] 点击失败 ({name}): {exc}")
            except Exception:
                continue
    log("[login] 未找到 Email sign-in code 按钮")
    return False


def _mouse_click_locator(page: Page, locator: Locator) -> None:
    box = locator.bounding_box(timeout=4000)
    if not box:
        raise RuntimeError("无法获取元素坐标")
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.wait_for_timeout(random.randint(80, 180))
    page.mouse.click(x, y)


def _cliclick_locator(page: Page, locator: Locator) -> None:
    box = locator.bounding_box(timeout=4000)
    if not box:
        raise RuntimeError("无法获取元素坐标")
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    screen = viewport_to_screen(x, y)
    if screen is None:
        raise RuntimeError("无法换算屏幕坐标")
    focus_chrome()
    if not cliclick_at(*screen):
        raise RuntimeError("cliclick 点击失败")


def click_button_by_label(page: Page, label: str) -> bool:
    for frame in iter_frames(page):
        for loc in _locators_for_label(frame, label):
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    if human_click_locator(page, loc.first, label=label):
                        return True
            except Exception:
                continue
    return False


def click_continue_button(page: Page) -> bool:
    clicked = click_button_by_label(page, "Continue")
    if not clicked:
        log("[login] 未找到 Continue 按钮")
    return clicked


def try_click_visible_turnstile(page: Page, label: str = "", rounds: int = 2) -> bool:
    """出现可见 Turnstile 复选框时用可信点击勾选（不劫持系统鼠标）；返回是否点过。"""
    clicked = False
    for _ in range(rounds):
        if find_visible_turnstile(page) is None:
            break
        log(f"[login] {label}检测到 Turnstile，可信点击勾选...")
        click_turnstile_checkbox(page)
        clicked = True
        page.wait_for_timeout(3000)
    return clicked


def is_on_code_input_page(page: Page) -> bool:
    return page.locator('[data-index="0"]').count() > 0


def wait_for_code_flow(page: Page, timeout: int = 12, *, attempt: int = 1) -> bool:
    """点击 Email sign-in code 后等待验证码页或离开 /password；必要时勾选可见的 Turnstile。"""
    log(f"[login] 等待验证码流程（第 {attempt} 次，最多 {timeout} 秒）...")
    deadline = time.time() + timeout
    turnstile_clicked_at: float | None = None
    post_turnstile_grace = 8
    last_log = 0.0
    start = time.time()

    while time.time() < deadline:
        now = time.time()
        remaining = int(deadline - now)

        if is_on_code_input_page(page):
            log(f"[login] 验证码输入框已出现（{int(now - start)}s）")
            return True
        if not is_password_url(page):
            log(f"[login] 已离开密码页（{int(now - start)}s）: {page.url}")
            return True

        # Turnstile 勾选后若超过 grace 秒仍无跳转，立即放弃本次等待并重试。
        if turnstile_clicked_at is not None and now - turnstile_clicked_at > post_turnstile_grace:
            log(f"[login] Turnstile 勾选后 {post_turnstile_grace}s 无跳转，放弃本次等待")
            return False

        # 仅首次出现可见复选框时点击一次，避免重复点击导致更长等待。
        if turnstile_clicked_at is None and find_visible_turnstile(page) is not None:
            log("[login] 出现可见 Turnstile 复选框，尝试可信点击勾选...")
            click_turnstile_checkbox(page)
            turnstile_clicked_at = now
            page.wait_for_timeout(1500)
            continue

        body = ""
        try:
            body = page.locator("body").inner_text(timeout=1000).lower()
        except Exception:
            pass
        if "can't verify" in body or "verify the user is human" in body:
            log("[login] Turnstile 校验失败（Can't verify the user is human）")
            return False
        # 注意: 不能用 "sign-in code" 判定，因为密码页的按钮文案就是 "Email sign-in code"，
        # 会导致点击前就误判为已进入验证码流程。
        if any(k in body for k in ("验证码", "verification code", "check your email", "enter the code")):
            log(f"[login] 页面提示已发送验证码（{int(now - start)}s）")
            return True

        if now - last_log >= 3:
            has_turnstile = turnstile_clicked_at is None and find_visible_turnstile(page) is not None
            preview = body.replace("\n", " ")[:80] if body else ""
            log(
                f"[login] 等待中... 剩余 {remaining}s "
                f"password={is_password_url(page)} turnstile={has_turnstile} "
                f"body={preview}"
            )
            last_log = now

        page.wait_for_timeout(500)

    log(f"[login] 等待验证码流程超时（{timeout}s），仍在 /password")
    return False


def choose_email_code_login(page: Page) -> None:
    """Submit email 后若仍在密码页，切换到验证码登录。"""
    page.wait_for_timeout(1000)

    if is_on_code_input_page(page):
        return

    on_password_page = is_password_url(page) or find_visible_locator(
        page, ['input[type="password"]'], timeout_ms=1500
    )
    if on_password_page:
        log("[login] 进入密码页，点击 Email sign-in code")
        # Cloudflare 交互式 Turnstile 失败时会显示 "Can't verify the user is human"
        # 并把内联复选框移除、退回表单，需要重新点击 Email sign-in code 再来一次。
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            if not click_email_code_button(page):
                save_debug(page, "email-code-button-missing")
                raise TimeoutError(
                    "密码页未找到 Email sign-in code 按钮。"
                    "请查看 debug/email-code-button-missing.png"
                )
            save_debug(page, f"after-email-code-click-{attempt}")
            if wait_for_code_flow(page, timeout=12, attempt=attempt):
                log("[login] 已进入验证码流程")
                return
            log(f"[login] 第 {attempt}/{max_attempts} 次未进入验证码流程，准备重试...")
            page.wait_for_timeout(random.randint(800, 1500))

        save_debug(page, "still-on-password-after-code-click")
        raise TimeoutError(
            "多次点击 Email sign-in code 后仍停留在 /password 页面（Cloudflare 交互式 "
            "Turnstile 校验失败）。请查看 debug/still-on-password-after-code-click.png"
        )

    if click_email_code_button(page):
        wait_for_code_flow(page, timeout=10, attempt=1)
        save_debug(page, "after-email-code-click")
        return

    if is_on_code_input_page(page):
        return

    save_debug(page, "no-code-flow-after-submit")
    log("[login] 提交邮箱后未进入验证码流程，继续等待验证码输入框...")


def abort_on_password_url(page: Page, phase: str) -> None:
    """等待过程中仍停留在 /password 视为异常，截图后退出，不再操作页面。"""
    label = f"stuck-on-password-{phase}"
    save_debug(page, label)
    raise TimeoutError(
        f"等待过程中仍停留在 /password 页面（phase={phase}）。"
        f"请查看 debug/{label}.png"
    )


def fill_email_and_submit(page: Page, email: str) -> None:
    open_login_page(page)
    wait_for_page_ready(page)

    if not fill_email_field(page, email):
        save_debug(page, "email-input-missing")
        raise TimeoutError("未找到邮箱输入框")

    log("[login] 点击 Continue 提交邮箱")
    if not click_continue_button(page):
        save_debug(page, "continue-button-missing")
        raise TimeoutError("未找到 Continue 按钮")

    # Continue 后可能立即出现 Cloudflare Turnstile（频繁尝试时尤甚）。用可信点击勾选，
    # 不再用 cliclick（会劫持系统鼠标且不可靠）。
    try_click_visible_turnstile(page, "Continue 后 ", rounds=3)

    choose_email_code_login(page)

    log(f"[login] 已提交邮箱，等待验证码输入框: {email}")


def _page_snapshot(page: Page) -> dict:
    title = page.title() or ""
    url = page.url
    body = ""
    try:
        body = page.locator("body").inner_text(timeout=5000)
    except Exception:
        pass
    has_code = page.locator('[data-index="0"]').count() > 0
    return {"title": title, "url": url, "body": body, "has_code": has_code}


def wait_for_login_progress(
    page: Page,
    timeout: int = 90,
    phase: str = "post-submit",
) -> bool:
    deadline = time.time() + timeout
    last_log = 0.0

    while time.time() < deadline:
        now = time.time()
        snap = _page_snapshot(page)

        if snap["has_code"]:
            log(f"[login] ({phase}) 验证码输入框已出现")
            return True

        if "cursor.com" in snap["url"] and "auth" not in snap["url"]:
            log(f"[login] ({phase}) 已进入 cursor.com: {snap['url']}")
            return True

        if page.get_by_text("Account Settings", exact=False).count() > 0:
            log(f"[login] ({phase}) 登录成功，进入 Account Settings")
            return True

        if is_password_url(page):
            abort_on_password_url(page, phase)

        body_lower = snap["body"].lower()
        if any(k in body_lower for k in ("check your email", "verification code", "enter the code", "验证码")):
            log(f"[login] ({phase}) 页面提示已发送验证码，等待输入框渲染...")

        if is_cloudflare_challenge(snap["title"].lower(), body_lower):
            log(f"[login] ({phase}) 提交后出现 Cloudflare，尝试可信点击勾选...")
            try_click_visible_turnstile(page, f"({phase}) ")
            page.wait_for_timeout(2000)
            continue

        if now - last_log >= 5:
            preview = snap["body"].replace("\n", " ")[:120]
            remaining = int(deadline - now)
            log(
                f"[login] ({phase}) 等待中... 剩余 {remaining}s "
                f"url={snap['url']} title={snap['title'][:40]} "
                f"code_input={snap['has_code']} body={preview}"
            )
            last_log = now

        page.wait_for_timeout(1500)

    log(f"[login] ({phase}) 等待超时 ({timeout}s)")
    save_debug(page, f"wait-timeout-{phase}")
    return False


def enter_verification_code(page: Page, code: str) -> None:
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"验证码格式错误: {code}")

    log(f"[login] 输入验证码: {code}")
    page.locator('[data-index="0"]').wait_for(state="visible", timeout=30000)

    for index, digit in enumerate(code):
        box = page.locator(f'[data-index="{index}"]')
        box.click()
        box.fill(digit)
        page.wait_for_timeout(random.randint(100, 300))


def login_with_email_code(context: BrowserContext, page: Page, email: str, password: str) -> None:
    mailbox = MailboxClient(email, password)
    mailbox_page = context.new_page()

    fill_email_and_submit(page, email)

    log("[login] 等待验证码输入框（最多 30 秒）...")
    if not wait_for_login_progress(page, timeout=30, phase="code-input"):
        log("[login] 仍未出现验证码框，再等待 10 秒...")
        try:
            page.locator('[data-index="0"]').wait_for(state="visible", timeout=10000)
        except Exception:
            save_debug(page, "code-input-missing")
            raise TimeoutError(
                "提交邮箱后未出现验证码输入框。可能 Cloudflare 未通过或邮箱被拦截。"
                "请查看 debug/code-input-missing.png"
            )

    log("[login] 正在从星辰邮箱大师获取验证码...")
    log(f"[login] 邮箱页面: {mailbox.frontend_url()}")
    try:
        code = mailbox.wait_for_code(timeout=180, interval=8, page=mailbox_page)
    finally:
        mailbox_page.close()

    enter_verification_code(page, code)

    log("[login] 等待登录完成（最多 60 秒）...")
    wait_for_login_progress(page, timeout=60, phase="post-code")
    page.wait_for_timeout(3000)


def export_cursor_cookies(context: BrowserContext, page: Page) -> str:
    print(f"[cookie] 访问 {CURSOR_URL} 读取 cookie...")
    page.goto(CURSOR_URL, wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(3000)

    page.goto(SETTINGS_URL, wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(3000)

    cookies = context.cookies()
    cursor_cookies = [
        c
        for c in cookies
        if "cursor" in (c.get("domain") or "") or "cursor" in (c.get("name") or "").lower()
    ]

    if not cursor_cookies:
        save_debug(page, "cookie-missing")
        raise RuntimeError("未获取到 cursor.com 相关 cookie，登录可能失败。")

    print(f"[cookie] 共获取 {len(cursor_cookies)} 个 cursor 相关 cookie")
    for item in cursor_cookies:
        name = item.get("name", "")
        domain = item.get("domain", "")
        preview = (item.get("value") or "")[:24]
        print(f"  - {name} @ {domain} = {preview}...")

    return print_cookie_editor_export(cursor_cookies)


def _install_cancel_debug_handler(page: Page) -> None:
    def on_cancel(signum: int, _frame) -> None:
        label = "cancelled" if signum == signal.SIGTERM else "interrupted"
        log(f"[debug] 收到终止信号 ({signum})，保存当前页面...")
        save_debug(page, label)
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, on_cancel)
    signal.signal(signal.SIGINT, on_cancel)


def run(credentials: str) -> str:
    log("[start] CursorCookie 登录脚本启动")
    email, password = parse_credentials(credentials)
    log(f"[start] 目标邮箱: {email}")

    with sync_playwright() as playwright:
        log("[browser] Playwright 已初始化")
        browser, context = launch_browser(playwright)
        # 持久化上下文启动时已自带一个空白页，直接复用，避免多出一个空标签。
        page = context.pages[0] if context.pages else context.new_page()
        log("[browser] 标签页已就绪")
        _install_cancel_debug_handler(page)

        try:
            login_with_email_code(context, page, email, password)
            return export_cursor_cookies(context, page)
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
    parser = argparse.ArgumentParser(description="Cursor 邮箱验证码登录并导出 Cookie")
    parser.add_argument(
        "credentials",
        nargs="?",
        default=os.environ.get("ACCOUNT_CREDENTIALS", ""),
        help="格式: 邮箱----密码",
    )
    args = parser.parse_args()

    if not args.credentials:
        print("错误: 请提供凭证，格式为 邮箱----密码", file=sys.stderr)
        print("示例: SapphiraCaelum5932@outlook.com----rq757721", file=sys.stderr)
        sys.exit(1)

    try:
        export_text = run(args.credentials)
        output_file = os.environ.get("COOKIE_OUTPUT_FILE", "cursor-cookies.json")
        Path(output_file).write_text(export_text, encoding="utf-8")
        print(f"[done] Cookie 已写入: {output_file}")
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
