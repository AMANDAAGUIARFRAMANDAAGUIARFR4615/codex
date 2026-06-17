"""Save page screenshots and HTML for debugging."""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page


def save_debug(page: Page, label: str) -> None:
    debug_dir = Path("debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    screenshot = debug_dir / f"{label}.png"
    html_file = debug_dir / f"{label}.html"

    try:
        page.screenshot(path=str(screenshot), full_page=True)
        print(f"[debug] 截图已保存: {screenshot}")
    except Exception as exc:
        print(f"[debug] 截图失败: {exc}")

    try:
        html_file.write_text(page.content(), encoding="utf-8")
        print(f"[debug] HTML 已保存: {html_file}")
        print(f"[debug] 当前 URL: {page.url}")
        print(f"[debug] 页面标题: {page.title()}")
    except Exception as exc:
        print(f"[debug] HTML 保存失败: {exc}")
