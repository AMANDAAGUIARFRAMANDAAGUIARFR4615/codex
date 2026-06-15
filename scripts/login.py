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

import capsolver_solver
import human_mouse
from cookie_export import print_cookie_editor_export
from debug_utils import save_debug
from io_utils import setup_utf8_stdio
from mailbox import MailboxClient
from turnstile_solver import (
    is_cloudflare_challenge,
    read_turnstile_token,
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
    # 放大窗口：runner 默认窗口仅 ~980x494，太小且不像真实桌面浏览器，是 Turnstile 风控
    # 信号之一。固定位置 (0,0) 便于真实光标(cliclick)的视口->屏幕坐标换算。
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
        if capsolver_solver.is_enabled():
            log("[browser] 检测到 CAPSOLVER_API_KEY，注入 Turnstile 回调 hook")
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
    if capsolver_solver.is_enabled():
        log("[browser] 检测到 CAPSOLVER_API_KEY，注入 Turnstile 回调 hook")
        capsolver_solver.install_hook(context)
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
            log("[login] 检测到 Cloudflare 验证，尝试用 CapSolver 自动处理...")
            try_solve_with_capsolver(page, "页面加载 ")
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
    # 拟真：先在页面上随机游走制造 telemetry，再点击输入框逐字符输入
    human_mouse.warm_up(page, label="填邮箱前")
    if not human_mouse.human_type(page, email_input, email, label="邮箱"):
        # 退化方案
        try:
            email_input.click()
            email_input.fill("")
            email_input.fill(email)
        except Exception as exc:
            log(f"[login] 邮箱输入失败: {exc}")
            return False
    page.wait_for_timeout(random.randint(300, 600))
    return True


def _locators_for_label(frame: Frame, label: str) -> list[Locator]:
    return [
        frame.get_by_role("button", name=label),
        frame.get_by_role("link", name=label),
        frame.locator(f'button:has-text("{label}")'),
        frame.locator(f'a:has-text("{label}")'),
    ]


def _human_click(page: Page, locator: Locator, label: str) -> None:
    if not human_mouse.human_click_locator(page, locator, label=label, timeout=6000):
        raise RuntimeError("human mouse 点击未成功")


def _mouse_click_locator(page: Page, locator: Locator) -> None:
    box = locator.bounding_box(timeout=3000)
    if not box:
        raise RuntimeError("无法获取元素坐标")
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.wait_for_timeout(random.randint(80, 180))
    page.mouse.click(x, y)


def _click_strategies(page: Page, locator: Locator, *, label: str = ""):
    """优先用人性化鼠标轨迹点击（给 Turnstile 喂真人 telemetry），失败再退化。"""
    return [
        ("human", lambda: _human_click(page, locator, label)),
        ("click", lambda: locator.click(timeout=6000)),
        ("mouse", lambda: _mouse_click_locator(page, locator)),
        ("js", lambda: locator.evaluate("el => el.click()", timeout=4000)),
    ]


def click_locator_humanlike(page: Page, locator: Locator, *, label: str = "") -> bool:
    try:
        locator.wait_for(state="visible", timeout=6000)
    except Exception as exc:
        log(f"[login] {label or '元素'} 不可见: {exc}")
        return False

    for name, action in _click_strategies(page, locator, label=label):
        try:
            action()
            log(f"[login] 已点击 {label or '元素'} (策略={name})")
            page.wait_for_timeout(random.randint(500, 1000))
            return True
        except Exception as exc:
            log(f"[login] 点击失败 (策略={name}): {exc}")
    return False


# 兼容旧调用名
human_click_locator = click_locator_humanlike


def click_email_code_button(page: Page) -> bool:
    label = "Email sign-in code"
    for frame in iter_frames(page):
        for loc in _locators_for_label(frame, label):
            try:
                if loc.count() == 0 or not loc.first.is_visible():
                    continue
                # 点击后按钮会变成 spinner（accessible name 改变），首次点击成功即返回，
                # 后续是否进入验证码页交给调用方等待。
                if click_locator_humanlike(page, loc.first, label=label):
                    return True
            except Exception:
                continue
    log("[login] 未找到 Email sign-in code 按钮")
    return False


def click_button_by_label(page: Page, label: str) -> bool:
    for frame in iter_frames(page):
        for loc in _locators_for_label(frame, label):
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    if click_locator_humanlike(page, loc.first, label=label):
                        return True
            except Exception:
                continue
    return False


def click_continue_button(page: Page) -> bool:
    clicked = click_button_by_label(page, "Continue")
    if not clicked:
        log("[login] 未找到 Continue 按钮")
    return clicked


def try_solve_with_capsolver(page: Page, label: str = "") -> bool:
    """用 CapSolver 求解页面上的 Turnstile 并注入 token；返回是否成功注入。

    这是当前唯一的 Turnstile 通过方式（已移除 cliclick 点击方案）。
    """
    if not capsolver_solver.is_enabled():
        log(f"[login] {label}⚠️ 未配置 CAPSOLVER_API_KEY，跳过 CapSolver 求解")
        return False

    params = capsolver_solver.detect_turnstile(page)
    if not params:
        log(f"[login] {label}页面未检测到 Turnstile，无需求解")
        return False

    log(
        f"[login] {label}检测到 Turnstile(sitekey={params['sitekey']})，"
        f"提交 CapSolver 求解（这一步会阻塞等待打码结果）..."
    )
    try:
        token = capsolver_solver.solve_turnstile(
            params["sitekey"],
            params["url"],
            action=params.get("action", ""),
            cdata=params.get("cdata", ""),
        )
    except Exception as exc:  # noqa: BLE001
        log(f"[login] {label}❌ CapSolver 求解失败: {exc}")
        return False

    if capsolver_solver.inject_token(page, token):
        log(f"[login] {label}✅ CapSolver token 已注入页面，等待应用继续...")
        page.wait_for_timeout(2000)
        return True

    log(f"[login] {label}❌ token 注入失败（页面无 response 字段/回调）")
    return False


def is_on_code_input_page(page: Page) -> bool:
    return page.locator('[data-index="0"]').count() > 0


def safe_body_text(page: Page, timeout_ms: int = 1000) -> str:
    try:
        return page.locator("body").inner_text(timeout=timeout_ms).lower()
    except Exception:
        return ""


def wait_for_turnstile_pass(page: Page, budget: int = 45, *, attempt: int = 1) -> bool:
    """点击 Email sign-in code 后用 CapSolver 通过 Turnstile，并确认进入下一步。

    流程：CapSolver 求解并注入 token（阻塞）-> 轮询确认是否进入验证码页 / 离开 /password。
    若注入后页面长时间不动，会在预算内重试一次 CapSolver（widget 可能被刷新重置）。
      成功：进入验证码页 / 离开 /password / 已发码提示
      失败：出现 "can't verify the user is human" 文案 / 超时
    """
    log(f"[login] 用 CapSolver 通过 Turnstile（第 {attempt} 次，预算 {budget}s）...")

    if not capsolver_solver.is_enabled():
        log("[login] ❌ 未配置 CAPSOLVER_API_KEY，无法自动通过 Turnstile")
        return False

    # 第一次求解并注入（阻塞等待打码结果）
    try_solve_with_capsolver(page, "等待阶段 ")

    deadline = time.time() + budget
    start = time.time()
    token_seen = False
    last_log = 0.0
    last_resolve = time.time()  # 控制重试 CapSolver 的节流

    while time.time() < deadline:
        now = time.time()
        elapsed = now - start

        if is_on_code_input_page(page):
            log(f"[login] ✅ 验证码输入框已出现（{elapsed:.1f}s）")
            return True
        if not is_password_url(page):
            log(f"[login] ✅ 已离开 /password（{elapsed:.1f}s）: {page.url}")
            return True

        token = read_turnstile_token(page)
        if token and not token_seen:
            token_seen = True
            log(f"[login] Turnstile token 已写入页面（len={len(token)}），等待应用跳转...")

        body = safe_body_text(page)
        if any(k in body for k in ("can't verify", "verify the user is human")):
            log(f"[login] ❌ Turnstile 校验失败提示（{elapsed:.1f}s）")
            return False
        if any(k in body for k in ("验证码", "verification code", "check your email", "enter the code")):
            log(f"[login] ✅ 页面提示已发送验证码（{elapsed:.1f}s）")
            return True

        # 注入后若 12s 仍无进展，重试一次 CapSolver（token 可能过期或 widget 已重置）
        if now - last_resolve >= 12:
            log(f"[login] {elapsed:.1f}s 仍无进展，重新调用 CapSolver 求解...")
            try_solve_with_capsolver(page, f"重试({elapsed:.0f}s) ")
            last_resolve = time.time()

        if now - last_log >= 3:
            log(
                f"[login] 等待中... {elapsed:.1f}s/{budget}s "
                f"token={'有' if token else '无'} url={page.url} "
                f"body={body.replace(chr(10), ' ')[:70]}"
            )
            last_log = now

        page.wait_for_timeout(500)

    log(f"[login] ⏰ Turnstile 等待超时（{budget}s），仍在 /password")
    return False


def choose_email_code_login(page: Page) -> None:
    """Submit email 后通过 Email sign-in code 的 Turnstile，进入验证码输入页。"""
    page.wait_for_timeout(800)

    if is_on_code_input_page(page):
        return

    on_password_page = is_password_url(page) or find_visible_locator(
        page, ['input[type="password"]'], timeout_ms=1500
    )
    if not on_password_page:
        if click_email_code_button(page):
            wait_for_turnstile_pass(page, budget=45, attempt=1)
            save_debug(page, "after-email-code-click")
        if is_on_code_input_page(page):
            return
        save_debug(page, "no-code-flow-after-submit")
        log("[login] 提交邮箱后未进入验证码流程，继续等待验证码输入框...")
        return

    log("[login] 进入密码页，准备点击 Email sign-in code 触发验证码")
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        # 每次点击前预热鼠标，制造连续的真人轨迹 telemetry
        human_mouse.warm_up(page, label=f"点码前#{attempt}")
        if not click_email_code_button(page):
            save_debug(page, "email-code-button-missing")
            raise TimeoutError(
                "密码页未找到 Email sign-in code 按钮。请查看 debug/email-code-button-missing.png"
            )
        save_debug(page, f"after-email-code-click-{attempt}")
        if wait_for_turnstile_pass(page, budget=45, attempt=attempt):
            log("[login] 已进入验证码流程")
            return
        log(f"[login] 第 {attempt}/{max_attempts} 次未通过 Turnstile")
        if attempt < max_attempts:
            log("[login] 刷新页面重置 Turnstile 后重试...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(random.randint(1200, 2200))
            except Exception as exc:
                log(f"[login] 刷新失败: {exc}")

    save_debug(page, "still-on-password-after-code-click")
    raise TimeoutError(
        "多次点击 Email sign-in code 后仍停留在 /password（Turnstile 未通过）。"
        "请查看 debug/still-on-password-after-code-click.png"
    )


def abort_on_password_url(page: Page, phase: str) -> None:
    """等待过程中仍停留在 /password 视为异常，截图后退出，不再操作页面。"""
    label = f"stuck-on-password-{phase}"
    save_debug(page, label)
    raise TimeoutError(
        f"等待过程中仍停留在 /password 页面（phase={phase}）。"
        f"请查看 debug/{label}.png"
    )


def setup_human_mouse(page: Page) -> None:
    """首屏就绪后：打印环境指纹、选择鼠标后端（默认真实光标 os）、计算坐标偏移。"""
    human_mouse.log_environment(page)
    backend = os.environ.get("HUMAN_MOUSE_BACKEND", "os")
    human_mouse.set_backend(backend)
    if human_mouse.get_backend() == "os":
        human_mouse.refresh_offset(page)


def fill_email_and_submit(page: Page, email: str) -> None:
    open_login_page(page)
    wait_for_page_ready(page)
    setup_human_mouse(page)

    if not fill_email_field(page, email):
        save_debug(page, "email-input-missing")
        raise TimeoutError("未找到邮箱输入框")

    log("[login] 点击 Continue 提交邮箱")
    if not click_continue_button(page):
        save_debug(page, "continue-button-missing")
        raise TimeoutError("未找到 Continue 按钮")

    # Continue 后可能立即出现 Cloudflare Turnstile（频繁尝试时尤甚），用 CapSolver 求解。
    try_solve_with_capsolver(page, "Continue 后 ")

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
            log(f"[login] ({phase}) 提交后出现 Cloudflare，尝试用 CapSolver 求解...")
            try_solve_with_capsolver(page, f"({phase}) ")
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
        # 关键：把默认超时压到 5s。否则页面跳转/跨域 iframe 未就绪时，bounding_box /
        # inner_text 等默认 30s 超时层层叠加，单次探测就能阻塞数分钟（实测吃掉整个预算）。
        page.set_default_timeout(5000)
        log("[browser] 标签页已就绪（默认超时 5s）")
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
