"""人性化鼠标/键盘轨迹。

Cloudflare Turnstile 的 managed 模式会采集鼠标移动轨迹、速度曲线、点击节奏等行为
telemetry 来判定真人 / 机器人。Playwright 的 ``locator.click`` 会把鼠标瞬移到元素中心
再点击，几乎没有轨迹，极易被判为机器人。

本模块用 ``page.mouse`` (经 CDP 派发，isTrusted=true) 生成：
  - 三次贝塞尔曲线轨迹（带随机控制点 -> 自然弧线）
  - ease-in-out 变速 + 逐点抖动
  - 偶发过冲再回拉（overshoot & correct）
  - 点击前悬停、按下随机保持时长
  - 逐字符输入（带节奏）

所有动作都打印详细日志，便于在 CI 日志里排查。
"""

from __future__ import annotations

import math
import random
import time

# 记录“虚拟光标”当前位置（page.mouse 内部位置无法读取，这里自行跟踪）。
_pos: dict[str, float | None] = {"x": None, "y": None}


def _log(msg: str) -> None:
    print(msg, flush=True)


def reset_position() -> None:
    _pos["x"] = None
    _pos["y"] = None


def get_position() -> tuple[float | None, float | None]:
    return _pos["x"], _pos["y"]


def viewport_size(page) -> tuple[float, float]:
    size = page.viewport_size
    if size and size.get("width") and size.get("height"):
        return float(size["width"]), float(size["height"])
    try:
        w = page.evaluate("() => window.innerWidth") or 1366
        h = page.evaluate("() => window.innerHeight") or 900
        return float(w), float(h)
    except Exception:
        return 1366.0, 900.0


def _ease_in_out(t: float) -> float:
    # 平滑加速再减速，模拟真人手部运动
    return 3 * t * t - 2 * t * t * t


def _cubic_bezier(p0, p1, p2, p3, t):
    mt = 1 - t
    x = (
        mt ** 3 * p0[0]
        + 3 * mt ** 2 * t * p1[0]
        + 3 * mt * t ** 2 * p2[0]
        + t ** 3 * p3[0]
    )
    y = (
        mt ** 3 * p0[1]
        + 3 * mt ** 2 * t * p1[1]
        + 3 * mt * t ** 2 * p2[1]
        + t ** 3 * p3[1]
    )
    return x, y


def _control_points(start, end):
    """在起终点之间生成两个带随机偏移的控制点，形成自然弧线。"""
    sx, sy = start
    ex, ey = end
    dist = math.hypot(ex - sx, ey - sy)
    # 垂直方向偏移量：距离越大弧度越明显，但加上限避免夸张
    bow = min(dist * random.uniform(0.08, 0.22), 120) * random.choice([-1, 1])
    dx, dy = ex - sx, ey - sy
    if dist < 1e-3:
        nx, ny = 0.0, 0.0
    else:
        nx, ny = -dy / dist, dx / dist  # 法向量
    c1 = (
        sx + dx * random.uniform(0.2, 0.4) + nx * bow * random.uniform(0.4, 0.8),
        sy + dy * random.uniform(0.2, 0.4) + ny * bow * random.uniform(0.4, 0.8),
    )
    c2 = (
        sx + dx * random.uniform(0.6, 0.8) + nx * bow * random.uniform(0.4, 0.8),
        sy + dy * random.uniform(0.6, 0.8) + ny * bow * random.uniform(0.4, 0.8),
    )
    return c1, c2


def _ensure_start(page) -> tuple[float, float]:
    if _pos["x"] is None or _pos["y"] is None:
        vw, vh = viewport_size(page)
        sx = random.uniform(vw * 0.3, vw * 0.7)
        sy = random.uniform(vh * 0.3, vh * 0.7)
        try:
            page.mouse.move(sx, sy)
        except Exception:
            pass
        _pos["x"], _pos["y"] = sx, sy
        _log(f"[mouse] 初始化光标位置 -> ({sx:.0f}, {sy:.0f})")
    return _pos["x"], _pos["y"]


def human_move(page, x: float, y: float, *, label: str = "", min_steps: int = 22) -> None:
    """沿贝塞尔曲线把鼠标从当前位置移动到 (x, y)。"""
    start = _ensure_start(page)
    end = (float(x), float(y))
    dist = math.hypot(end[0] - start[0], end[1] - start[1])
    c1, c2 = _control_points(start, end)

    # 步数随距离增加；每步之间小睡，整体 ~0.18-0.7s
    steps = int(min_steps + min(dist / 6.0, 60))
    steps = max(steps, min_steps)
    t0 = time.time()
    for i in range(1, steps + 1):
        t = _ease_in_out(i / steps)
        px, py = _cubic_bezier(start, c1, c2, end, t)
        # 逐点抖动，越接近终点抖动越小
        jitter = max(0.0, (1 - t) * 1.6)
        px += random.uniform(-jitter, jitter)
        py += random.uniform(-jitter, jitter)
        try:
            page.mouse.move(px, py)
        except Exception as exc:
            _log(f"[mouse] move 失败: {exc}")
            break
        time.sleep(random.uniform(0.004, 0.018))

    # 偶发过冲再回拉：真人常略微越过目标再修正
    if dist > 60 and random.random() < 0.5:
        ox = end[0] + random.uniform(-6, 6)
        oy = end[1] + random.uniform(-6, 6)
        try:
            page.mouse.move(ox, oy)
            time.sleep(random.uniform(0.03, 0.08))
            page.mouse.move(end[0], end[1])
        except Exception:
            pass

    _pos["x"], _pos["y"] = end
    _log(
        f"[mouse] 移动{(' '+label) if label else ''}: "
        f"({start[0]:.0f},{start[1]:.0f}) -> ({end[0]:.0f},{end[1]:.0f}) "
        f"dist={dist:.0f} steps={steps} {1000*(time.time()-t0):.0f}ms"
    )


def human_click_xy(page, x: float, y: float, *, label: str = "") -> None:
    """移动到 (x,y) 后做一次拟真点击。"""
    human_move(page, x, y, label=label)
    time.sleep(random.uniform(0.05, 0.16))  # 到位后的短暂停顿
    try:
        page.mouse.down()
        time.sleep(random.uniform(0.045, 0.12))  # 按下保持
        page.mouse.up()
        _log(f"[mouse] 点击{(' '+label) if label else ''} @ ({x:.0f},{y:.0f})")
    except Exception as exc:
        _log(f"[mouse] 点击失败{(' '+label) if label else ''}: {exc}")
        raise


def _point_in_box(box: dict) -> tuple[float, float]:
    """在元素包围盒内取一个偏中心的随机点（避免每次都点正中）。"""
    x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
    y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
    return x, y


def human_move_to_locator(page, locator, *, label: str = "", timeout: int = 6000) -> tuple[float, float] | None:
    locator.scroll_into_view_if_needed(timeout=timeout)
    box = locator.bounding_box(timeout=timeout)
    if not box:
        _log(f"[mouse] 无法获取 {label or '元素'} 包围盒")
        return None
    x, y = _point_in_box(box)
    human_move(page, x, y, label=label)
    return x, y


def human_click_locator(page, locator, *, label: str = "", timeout: int = 6000) -> bool:
    """拟真移动到 locator 上并点击。"""
    try:
        locator.wait_for(state="visible", timeout=timeout)
    except Exception as exc:
        _log(f"[mouse] {label or '元素'} 不可见: {exc}")
        return False
    point = human_move_to_locator(page, locator, label=label, timeout=timeout)
    if point is None:
        return False
    time.sleep(random.uniform(0.05, 0.16))
    try:
        page.mouse.down()
        time.sleep(random.uniform(0.045, 0.12))
        page.mouse.up()
        _log(f"[mouse] 点击 {label or '元素'} @ ({point[0]:.0f},{point[1]:.0f})")
        return True
    except Exception as exc:
        _log(f"[mouse] 点击 {label or '元素'} 失败: {exc}")
        return False


def human_type(page, locator, text: str, *, label: str = "", timeout: int = 6000) -> bool:
    """先拟真点击聚焦输入框，再逐字符输入（带节奏）。"""
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
        time.sleep(random.uniform(0.04, 0.16))
    _log(f"[mouse] 已输入 {label or '文本'}（{len(text)} 字符）")
    return True


def warm_up(page, *, moves: int = 0, label: str = "") -> None:
    """在视口内做几次随机游走，产生自然的鼠标 telemetry。"""
    if moves <= 0:
        moves = random.randint(2, 4)
    vw, vh = viewport_size(page)
    _log(f"[mouse] 预热{(' '+label) if label else ''}: {moves} 段随机游走 (viewport {vw:.0f}x{vh:.0f})")
    for _ in range(moves):
        tx = random.uniform(vw * 0.2, vw * 0.8)
        ty = random.uniform(vh * 0.2, vh * 0.75)
        human_move(page, tx, ty, label="预热")
        time.sleep(random.uniform(0.08, 0.25))


def hover_jitter(page, *, around: tuple[float, float] | None = None, label: str = "") -> None:
    """在某点附近做轻微移动，模拟真人等待时手的微动（用于 Turnstile 校验窗口期）。"""
    cx, cy = get_position()
    if around is not None:
        cx, cy = around
    if cx is None or cy is None:
        vw, vh = viewport_size(page)
        cx, cy = vw / 2, vh / 2
    tx = cx + random.uniform(-40, 40)
    ty = cy + random.uniform(-30, 30)
    human_move(page, tx, ty, label=label or "微动", min_steps=10)
