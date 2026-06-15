"""macOS 上用 cliclick 识别 Turnstile 位置并模拟系统级点击。"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import time

import human_mouse
from debug_utils import save_debug
from io_utils import setup_utf8_stdio

setup_utf8_stdio()

CHROME_UI_OFFSET = int(os.environ.get("CHROME_UI_OFFSET", "88"))


def _log(msg: str) -> None:
    print(msg, flush=True)


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


_TURNSTILE_SELECTORS = [
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[src*="turnstile"]',
    'iframe[title*="Cloudflare" i]',
    'iframe[title*="human" i]',
    'iframe[title*="widget" i]',
    ".cf-turnstile",
    "#cf-turnstile",
    "[data-sitekey]",
]


# 所有探测用的 bounding_box 都使用很短的超时，避免页面跳转/跨域 iframe 未就绪时
# 默认 30s 超时层层叠加，导致单次探测阻塞数分钟（实测会吃掉整个等待预算）。
_PROBE_TIMEOUT_MS = 1200


def _candidate_boxes(page) -> list[dict]:
    """收集所有可能是 Turnstile 组件的视口包围盒。

    Turnstile 的可见复选框常位于跨域/影子 iframe 内，page.frames 与序列化 HTML 都不一定
    可靠（尤其在 patchright 隔离环境下），因此综合多种来源：frame 列表、选择器命中的
    容器/iframe、以及兜底的所有 iframe。

    注意：每个 bounding_box 都带短超时（_PROBE_TIMEOUT_MS），否则一次探测可能阻塞数分钟。
    """
    boxes: list[dict] = []

    for fr in page.frames:
        url = (fr.url or "").lower()
        if "challenges.cloudflare.com" in url or "turnstile" in url:
            try:
                handle = fr.frame_element()
                box = handle.bounding_box()
                if box:
                    boxes.append(box)
            except Exception:
                pass

    for selector in _TURNSTILE_SELECTORS:
        try:
            locator = page.locator(selector)
            count = min(locator.count(), 3)
        except Exception:
            count = 0
        for i in range(count):
            try:
                box = locator.nth(i).bounding_box(timeout=_PROBE_TIMEOUT_MS)
                if box:
                    boxes.append(box)
            except Exception:
                pass

    try:
        for handle in page.query_selector_all("iframe"):
            try:
                box = handle.bounding_box()
                if box:
                    boxes.append(box)
            except Exception:
                pass
    except Exception:
        pass

    return boxes


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


def find_visible_turnstile(page):
    """返回可见 Turnstile 复选框的点击坐标与包围盒: (cx, cy, box)。

    过滤隐藏/全屏的工具 iframe（如 top:-100vh、整屏遮罩），并用命中测试确保该坐标处
    最顶层确实是 Turnstile（iframe 或 .cf-turnstile 容器），避免点到被背景层遮挡的副本。
    """
    seen: set[tuple[int, int, int, int]] = set()
    for box in _candidate_boxes(page):
        key = (int(box["x"]), int(box["y"]), int(box["width"]), int(box["height"]))
        if key in seen:
            continue
        seen.add(key)

        if not (40 <= box["width"] <= 700):
            continue
        if not (20 <= box["height"] <= 220):
            continue

        # 复选框位置：左侧、纵向居中
        cx = box["x"] + min(30.0, box["width"] * 0.12)
        cy = box["y"] + box["height"] / 2
        if cx < 1 or cy < 1:
            continue

        try:
            ok = page.evaluate(
                "([x, y]) => { const e = document.elementFromPoint(x, y);"
                " if (!e) return false;"
                " if (e.tagName === 'IFRAME') return true;"
                " return !!(e.closest && (e.closest('.cf-turnstile') || e.closest('#cf-turnstile'))); }",
                [cx, cy],
            )
        except Exception:
            ok = True
        if not ok:
            continue

        return cx, cy, box
    return None


def click_turnstile_checkbox(page) -> bool:
    """用人性化轨迹 + CDP 可信事件勾选可见的 Turnstile 复选框。

    page.mouse 经 CDP 派发，事件 isTrusted=true；按视口坐标点击会被浏览器路由进跨域/影子
    iframe 命中复选框，且与屏幕分辨率无关。这里复用 human_mouse 的贝塞尔轨迹，使交互式
    校验的鼠标 telemetry 看起来像真人。
    """
    found = find_visible_turnstile(page)
    if found is None:
        _log("[turnstile] 未发现可见的 Turnstile 复选框")
        return False

    cx, cy, box = found
    _log(f"[turnstile] 发现可见 Turnstile: {box} -> 点击 ({cx:.0f},{cy:.0f})")
    try:
        human_mouse.human_click_xy(page, cx, cy, label="Turnstile 复选框")
        return True
    except Exception as exc:
        _log(f"[turnstile] 点击失败: {exc}")
        return False


def focus_chrome() -> None:
    if sys.platform != "darwin":
        return
    subprocess.run(
        ["osascript", "-e", 'tell application "Google Chrome" to activate'],
        check=False,
    )


def position_chrome_window() -> None:
    if sys.platform != "darwin":
        return
    script = """
    tell application "Google Chrome"
        activate
        if (count of windows) > 0 then
            set bounds of front window to {80, 80, 1446, 980}
        end if
    end tell
    """
    subprocess.run(["osascript", "-e", script], check=False)
    time.sleep(0.5)


def get_chrome_window_origin() -> tuple[int, int] | None:
    if sys.platform != "darwin":
        return None

    script = """
    tell application "Google Chrome"
        if (count of windows) > 0 then
            set p to position of front window
            return (item 1 of p as string) & "," & (item 2 of p as string)
        end if
    end tell
    """
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    text = result.stdout.strip()
    if "," not in text:
        return None

    x_str, y_str = text.split(",", 1)
    try:
        return int(x_str), int(y_str)
    except ValueError:
        return None


def cliclick_at(x: int, y: int) -> bool:
    cliclick_bin = os.environ.get("CLICLICK_PATH", "cliclick")
    result = subprocess.run(
        [cliclick_bin, f"c:{x},{y}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _log(f"[turnstile] cliclick 失败: {result.stderr.strip()}")
        return False

    _log(f"[turnstile] cliclick 点击屏幕坐标 ({x}, {y})")
    return True


def viewport_to_screen(vx: float, vy: float) -> tuple[int, int] | None:
    origin = get_chrome_window_origin()
    if origin is None:
        return None
    wx, wy = origin
    return int(wx + vx), int(wy + CHROME_UI_OFFSET + vy)


def _points_from_box(box: dict) -> list[tuple[float, float]]:
    x = box["x"]
    y = box["y"]
    w = box["width"]
    h = box["height"]
    return [
        (x + min(28, w * 0.12), y + h / 2),
        (x + w * 0.18, y + h / 2),
        (x + w / 2, y + h / 2),
    ]


def find_turnstile_viewport_points(page) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    selectors = [
        'iframe[src*="challenges.cloudflare.com"]',
        'iframe[src*="turnstile"]',
        'iframe[title*="Cloudflare"]',
        'iframe[title*="widget"]',
        ".cf-turnstile",
        "#cf-turnstile",
        "[data-sitekey]",
    ]

    for selector in selectors:
        locator = page.locator(selector)
        if locator.count() == 0:
            continue
        try:
            box = locator.first.bounding_box()
            if box and box.get("width", 0) > 10 and box.get("height", 0) > 10:
                points.extend(_points_from_box(box))
                _log(f"[turnstile] 识别到组件 {selector}: {box}")
        except Exception:
            continue

    if points:
        return points

    viewport = page.viewport_size or {"width": 1366, "height": 900}
    width = viewport.get("width", 1366)
    height = viewport.get("height", 900)
    _log("[turnstile] 未找到 Turnstile 节点，使用页面中心区域启发式坐标")
    return [
        (width * 0.42, height * 0.52),
        (width * 0.50, height * 0.55),
        (width * 0.38, height * 0.48),
    ]


def try_cliclick_turnstile(page) -> bool:
    if sys.platform != "darwin":
        _log("[turnstile] cliclick 仅支持 macOS")
        return False

    focus_chrome()
    position_chrome_window()
    page.wait_for_timeout(800)

    viewport_points = find_turnstile_viewport_points(page)
    clicked = False

    for vx, vy in viewport_points:
        screen = viewport_to_screen(vx, vy)
        if screen is None:
            continue
        sx, sy = screen
        for dx, dy in ((0, 0), (8, 0), (-8, 0), (0, 6), (0, -6)):
            if cliclick_at(sx + dx, sy + dy):
                clicked = True
                page.wait_for_timeout(2500)
                break
        if clicked:
            break

    return clicked


def challenge_cleared(page) -> bool:
    title = (page.title() or "").lower()
    body = ""
    try:
        body = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        pass
    return not is_cloudflare_challenge(title, body)


def handle_cloudflare(page, *, max_attempts: int = 6) -> bool:
    title = (page.title() or "").lower()
    body = ""
    try:
        body = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        pass

    if not is_cloudflare_challenge(title, body):
        return False

    save_debug(page, "cloudflare-challenge")

    for attempt in range(1, max_attempts + 1):
        _log(f"[turnstile] cliclick 自动过验证 ({attempt}/{max_attempts})...")
        try_cliclick_turnstile(page)
        page.wait_for_timeout(4000)

        if challenge_cleared(page):
            _log("[turnstile] Cloudflare 验证已通过")
            return True

        save_debug(page, f"cloudflare-attempt-{attempt}")
        page.wait_for_timeout(2000)

    save_debug(page, "cloudflare-failed")
    _log("[turnstile] cliclick 未能通过 Cloudflare 验证")
    return False
