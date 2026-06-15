"""人性化鼠标/键盘轨迹（双后端）。

Cloudflare Turnstile 的交互式校验会采集鼠标移动轨迹、速度曲线、点击节奏，并且能识别
合成事件。两个后端：

  - ``os``  : 用 cliclick 驱动**真实 macOS 光标**（CGEvent，硬件级），一次子进程内用
              ``m:`` + ``w:`` 串起整条贝塞尔轨迹再 ``c:`` 点击。最接近真人手动操作。
  - ``cdp`` : 用 ``page.mouse``（CDP 可信事件，isTrusted=true）。跨平台兜底；不移动真实
              光标，交互式 Turnstile 更易识别。

要点：runner 上每次 page.mouse / 子进程调用都有 ~50ms 延迟，所以轨迹点数要少（8-16），
否则一次移动会变成数秒的机器人式慢拖。所有动作打印详细日志。
"""

from __future__ import annotations

import math
import os
import random
import subprocess
import sys
import time

_backend = "cdp"
_pos: dict[str, float | None] = {"x": None, "y": None}          # CDP 虚拟光标（视口坐标）
_os_pos: dict[str, float | None] = {"x": None, "y": None}        # 真实光标（屏幕坐标）
_offset: dict[str, float] | None = None                          # 视口->屏幕偏移缓存


def _log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# 后端管理
# --------------------------------------------------------------------------- #
def cliclick_path() -> str:
    return os.environ.get("CLICLICK_PATH", "cliclick")


def os_mouse_available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        r = subprocess.run([cliclick_path(), "-V"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def set_backend(name: str) -> None:
    global _backend
    if name == "os" and not os_mouse_available():
        _log("[mouse] 请求 os 后端但 cliclick 不可用，回退 cdp")
        name = "cdp"
    _backend = name
    if name == "os":
        _focus_chrome()
    _log(f"[mouse] 鼠标后端 = {name}")


def get_backend() -> str:
    return _backend


def _focus_chrome() -> None:
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to activate'],
            check=False,
            timeout=5,
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 几何
# --------------------------------------------------------------------------- #
def viewport_size(page) -> tuple[float, float]:
    try:
        wh = page.evaluate("() => [window.innerWidth, window.innerHeight]")
        if wh and wh[0] and wh[1]:
            return float(wh[0]), float(wh[1])
    except Exception:
        pass
    size = page.viewport_size
    if size and size.get("width"):
        return float(size["width"]), float(size["height"])
    return 1366.0, 900.0


def refresh_offset(page) -> dict[str, float] | None:
    """计算视口坐标 -> 屏幕坐标 的偏移：screen = (ox + vx, oy + vy)。

    用 window.screenX/Y + (outer-inner) 动态求浏览器 chrome 高度，比硬编码 88 更可靠。
    """
    global _offset
    try:
        geo = page.evaluate(
            "() => ({sx: window.screenX, sy: window.screenY,"
            " ow: window.outerWidth, oh: window.outerHeight,"
            " iw: window.innerWidth, ih: window.innerHeight,"
            " dpr: window.devicePixelRatio})"
        )
    except Exception as exc:
        _log(f"[mouse] 读取窗口几何失败: {exc}")
        return _offset
    side = max(0.0, (geo["ow"] - geo["iw"]) / 2)
    ox = geo["sx"] + side
    oy = geo["sy"] + (geo["oh"] - geo["ih"]) - side
    _offset = {"ox": ox, "oy": oy, "dpr": geo.get("dpr", 1)}
    _log(
        f"[mouse] 窗口几何 screenXY=({geo['sx']},{geo['sy']}) "
        f"outer={geo['ow']}x{geo['oh']} inner={geo['iw']}x{geo['ih']} "
        f"dpr={geo['dpr']} -> 偏移 ox={ox:.0f} oy={oy:.0f}"
    )
    return _offset


def _to_screen(page, vx: float, vy: float) -> tuple[float, float]:
    off = _offset or refresh_offset(page)
    if not off:
        return vx, vy
    return off["ox"] + vx, off["oy"] + vy


def _ease_in_out(t: float) -> float:
    return 3 * t * t - 2 * t * t * t


def _cubic_bezier(p0, p1, p2, p3, t):
    mt = 1 - t
    x = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
    y = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
    return x, y


def _control_points(start, end):
    sx, sy = start
    ex, ey = end
    dist = math.hypot(ex - sx, ey - sy)
    bow = min(dist * random.uniform(0.08, 0.2), 90) * random.choice([-1, 1])
    dx, dy = ex - sx, ey - sy
    if dist < 1e-3:
        nx, ny = 0.0, 0.0
    else:
        nx, ny = -dy / dist, dx / dist
    c1 = (sx + dx * random.uniform(0.2, 0.4) + nx * bow, sy + dy * random.uniform(0.2, 0.4) + ny * bow)
    c2 = (sx + dx * random.uniform(0.6, 0.8) + nx * bow, sy + dy * random.uniform(0.6, 0.8) + ny * bow)
    return c1, c2


def _bezier_points(start, end, n):
    c1, c2 = _control_points(start, end)
    pts = []
    for i in range(1, n + 1):
        t = _ease_in_out(i / n)
        x, y = _cubic_bezier(start, c1, c2, end, t)
        jit = max(0.0, (1 - t) * 1.5)
        pts.append((x + random.uniform(-jit, jit), y + random.uniform(-jit, jit)))
    return pts


def _n_steps(dist: float) -> int:
    # runner 上每点 ~50ms，控制总时长在 ~0.3-0.8s
    return max(6, min(16, int(dist / 45) + 6))


def reset_position() -> None:
    _pos["x"] = _pos["y"] = None


def get_position() -> tuple[float | None, float | None]:
    return _pos["x"], _pos["y"]


# --------------------------------------------------------------------------- #
# OS 后端（真实光标）
# --------------------------------------------------------------------------- #
def _os_current_screen() -> tuple[float, float] | None:
    if _os_pos["x"] is not None:
        return _os_pos["x"], _os_pos["y"]
    try:
        r = subprocess.run([cliclick_path(), "p:"], capture_output=True, text=True, timeout=5)
        txt = r.stdout.strip()
        if "," in txt:
            x, y = txt.split(",", 1)
            return float(x), float(y)
    except Exception:
        pass
    return None


def _os_gesture(page, vx: float, vy: float, *, click: bool, label: str) -> bool:
    ex, ey = _to_screen(page, vx, vy)
    start = _os_current_screen()
    if start is None:
        start = (ex - 60, ey - 40)
    dist = math.hypot(ex - start[0], ey - start[1])
    pts = _bezier_points(start, (ex, ey), _n_steps(dist))

    cmd = [cliclick_path()]
    for px, py in pts:
        cmd.append(f"m:{int(round(px))},{int(round(py))}")
        cmd.append(f"w:{random.randint(8, 22)}")
    if click:
        cmd.append(f"c:{int(round(ex))},{int(round(ey))}")
    else:
        cmd.append(f"m:{int(round(ex))},{int(round(ey))}")

    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception as exc:
        _log(f"[mouse:os] cliclick 异常: {exc}")
        return False
    _os_pos["x"], _os_pos["y"] = ex, ey
    ok = r.returncode == 0
    _log(
        f"[mouse:os] {'点击' if click else '移动'}{(' '+label) if label else ''}: "
        f"屏幕({start[0]:.0f},{start[1]:.0f})->({ex:.0f},{ey:.0f}) 视口({vx:.0f},{vy:.0f}) "
        f"dist={dist:.0f} pts={len(pts)} {1000*(time.time()-t0):.0f}ms rc={r.returncode}"
    )
    if not ok and r.stderr:
        _log(f"[mouse:os] stderr: {r.stderr.strip()[:160]}")
    _pos["x"], _pos["y"] = vx, vy
    return ok


# --------------------------------------------------------------------------- #
# CDP 后端（合成可信事件）
# --------------------------------------------------------------------------- #
def _cdp_ensure_start(page) -> tuple[float, float]:
    if _pos["x"] is None:
        vw, vh = viewport_size(page)
        sx, sy = random.uniform(vw * 0.3, vw * 0.7), random.uniform(vh * 0.3, vh * 0.7)
        try:
            page.mouse.move(sx, sy)
        except Exception:
            pass
        _pos["x"], _pos["y"] = sx, sy
    return _pos["x"], _pos["y"]


def _cdp_move(page, vx: float, vy: float, *, label: str) -> None:
    start = _cdp_ensure_start(page)
    end = (vx, vy)
    dist = math.hypot(end[0] - start[0], end[1] - start[1])
    pts = _bezier_points(start, end, _n_steps(dist))
    t0 = time.time()
    for px, py in pts:
        try:
            page.mouse.move(px, py)
        except Exception as exc:
            _log(f"[mouse:cdp] move 失败: {exc}")
            break
        if random.random() < 0.15:
            time.sleep(random.uniform(0.012, 0.035))
    _pos["x"], _pos["y"] = end
    _log(
        f"[mouse:cdp] 移动{(' '+label) if label else ''}: "
        f"({start[0]:.0f},{start[1]:.0f})->({end[0]:.0f},{end[1]:.0f}) "
        f"dist={dist:.0f} pts={len(pts)} {1000*(time.time()-t0):.0f}ms"
    )


def _cdp_click(page, vx: float, vy: float, *, label: str) -> None:
    _cdp_move(page, vx, vy, label=label)
    time.sleep(random.uniform(0.05, 0.14))
    page.mouse.down()
    time.sleep(random.uniform(0.045, 0.11))
    page.mouse.up()
    _log(f"[mouse:cdp] 点击{(' '+label) if label else ''} @ ({vx:.0f},{vy:.0f})")


# --------------------------------------------------------------------------- #
# 公共 API（自动选后端）
# --------------------------------------------------------------------------- #
def human_move(page, x: float, y: float, *, label: str = "") -> None:
    if _backend == "os":
        _os_gesture(page, x, y, click=False, label=label)
    else:
        _cdp_move(page, x, y, label=label)


def human_click_xy(page, x: float, y: float, *, label: str = "") -> None:
    if _backend == "os":
        if not _os_gesture(page, x, y, click=True, label=label):
            raise RuntimeError("os 点击失败")
    else:
        _cdp_click(page, x, y, label=label)


def _point_in_box(box: dict) -> tuple[float, float]:
    x = box["x"] + box["width"] * random.uniform(0.4, 0.6)
    y = box["y"] + box["height"] * random.uniform(0.4, 0.6)
    return x, y


def human_click_locator(page, locator, *, label: str = "", timeout: int = 6000) -> bool:
    try:
        locator.wait_for(state="visible", timeout=timeout)
        locator.scroll_into_view_if_needed(timeout=timeout)
        box = locator.bounding_box(timeout=timeout)
    except Exception as exc:
        _log(f"[mouse] {label or '元素'} 不可点击: {exc}")
        return False
    if not box:
        _log(f"[mouse] 无法获取 {label or '元素'} 包围盒")
        return False
    x, y = _point_in_box(box)
    try:
        human_click_xy(page, x, y, label=label)
        return True
    except Exception as exc:
        _log(f"[mouse] 点击 {label or '元素'} 失败: {exc}")
        return False


def human_type(page, locator, text: str, *, label: str = "", timeout: int = 6000) -> bool:
    if not human_click_locator(page, locator, label=label or "输入框", timeout=timeout):
        return False
    try:
        locator.fill("")
    except Exception:
        pass
    time.sleep(random.uniform(0.08, 0.2))
    for ch in text:
        try:
            page.keyboard.type(ch)
        except Exception as exc:
            _log(f"[mouse] 输入失败: {exc}")
            return False
        time.sleep(random.uniform(0.04, 0.14))
    _log(f"[mouse] 已输入 {label or '文本'}（{len(text)} 字符）")
    return True


def warm_up(page, *, moves: int = 0, label: str = "") -> None:
    if moves <= 0:
        moves = random.randint(2, 3)
    vw, vh = viewport_size(page)
    _log(f"[mouse] 预热{(' '+label) if label else ''}: {moves} 段 (viewport {vw:.0f}x{vh:.0f}, 后端={_backend})")
    for _ in range(moves):
        tx = random.uniform(vw * 0.2, vw * 0.8)
        ty = random.uniform(vh * 0.2, vh * 0.75)
        human_move(page, tx, ty, label="预热")
        time.sleep(random.uniform(0.06, 0.2))


def hover_jitter(page, *, around: tuple[float, float] | None = None, label: str = "") -> None:
    if around is not None:
        cx, cy = around
    else:
        cx, cy = get_position()
    if cx is None or cy is None:
        vw, vh = viewport_size(page)
        cx, cy = vw / 2, vh / 2
    tx = cx + random.uniform(-35, 35)
    ty = cy + random.uniform(-25, 25)
    human_move(page, tx, ty, label=label or "微动")


def log_environment(page) -> None:
    """打印浏览器指纹/环境信息，便于排查 Turnstile 风控（屏幕、UA、webdriver 等）。"""
    try:
        info = page.evaluate(
            "() => ({"
            " ua: navigator.userAgent, wd: navigator.webdriver,"
            " plat: navigator.platform, langs: navigator.languages,"
            " hc: navigator.hardwareConcurrency, mem: navigator.deviceMemory,"
            " screen: [screen.width, screen.height, screen.availWidth, screen.availHeight],"
            " win: [window.outerWidth, window.outerHeight, window.innerWidth, window.innerHeight],"
            " pos: [window.screenX, window.screenY], dpr: window.devicePixelRatio,"
            " plugins: navigator.plugins.length})"
        )
        _log(f"[env] {info}")
    except Exception as exc:
        _log(f"[env] 读取失败: {exc}")
