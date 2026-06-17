"""通过 Cookie-Editor 扩展或 Playwright API 导入 Cookie-Editor JSON。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page

SAMESITE_TO_PLAYWRIGHT = {
    "no_restriction": "None",
    "lax": "Lax",
    "strict": "Strict",
    "unspecified": "Lax",
}

POPUP_PATH = "interface/popup/cookie-list.html"


def log(msg: str) -> None:
    print(msg, flush=True)


def load_cookie_editor_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Cookie 文件应为 JSON 数组: {path}")
    if not data:
        raise ValueError(f"Cookie 文件为空: {path}")
    return data


def cookie_editor_to_playwright(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for cookie in cookies:
        domain = cookie.get("domain", "")
        if not domain or not cookie.get("name"):
            continue

        item: dict[str, Any] = {
            "name": cookie["name"],
            "value": cookie.get("value", ""),
            "domain": domain,
            "path": cookie.get("path") or "/",
            "httpOnly": bool(cookie.get("httpOnly", False)),
            "secure": bool(cookie.get("secure", False)),
        }

        if not cookie.get("session") and cookie.get("expirationDate"):
            item["expires"] = float(cookie["expirationDate"])

        same_site = cookie.get("sameSite")
        if same_site:
            item["sameSite"] = SAMESITE_TO_PLAYWRIGHT.get(str(same_site), "Lax")

        converted.append(item)

    if not converted:
        raise ValueError("未解析到有效 cookie")
    return converted


def extension_id_from_path(extension_dir: Path) -> str:
    """未打包扩展 ID = SHA256(绝对路径) 前 16 字节，按 a-p 编码。"""
    digest = hashlib.sha256(str(extension_dir.resolve()).encode()).digest()[:16]
    return "".join(chr(ord("a") + (byte >> 4)) + chr(ord("a") + (byte & 0x0F)) for byte in digest)


def get_extension_id(context: BrowserContext, timeout: int = 30) -> str:
    extension_dir = os.environ.get("COOKIE_EDITOR_DIR", "").strip()
    if extension_dir:
        expected = extension_id_from_path(Path(extension_dir))
    else:
        expected = ""

    deadline = time.time() + timeout
    while time.time() < deadline:
        for worker in context.service_workers:
            worker_url = worker.url or ""
            if "cookie-editor.js" not in worker_url:
                continue
            ext_id = worker_url.split("/")[2]
            if ext_id:
                log(f"[cookie] 检测到 Cookie-Editor service worker: {worker_url}")
                return ext_id
        time.sleep(0.5)

    if expected:
        log(
            "[cookie] 未检测到 Cookie-Editor service worker，"
            f"使用路径推导 ID: {expected}"
        )
        return expected

    raise RuntimeError(
        "Cookie-Editor 扩展未加载。"
        "系统 Google Chrome 不支持 --load-extension，请改用 patchright Chromium。"
    )


def import_via_cookie_editor(
    context: BrowserContext,
    cookie_json: str,
    *,
    timeout_ms: int = 60_000,
) -> None:
    ext_id = get_extension_id(context)
    popup_url = f"chrome-extension://{ext_id}/{POPUP_PATH}"
    log(f"[cookie] 打开 Cookie-Editor 弹窗: {popup_url}")

    popup = context.new_page()
    try:
        popup.goto(popup_url, wait_until="domcontentloaded", timeout=timeout_ms)
        popup.wait_for_timeout(1500)

        popup.locator("#import-cookies").click(timeout=timeout_ms)
        textarea = popup.locator("#content-import")
        textarea.wait_for(state="visible", timeout=timeout_ms)
        textarea.fill(cookie_json)

        popup.locator("#save-import-cookie").click(timeout=timeout_ms)
        popup.wait_for_timeout(2500)

        icon_href = popup.locator("#save-import-cookie use").get_attribute("href") or ""
        if "times" in icon_href:
            raise RuntimeError("Cookie-Editor 导入失败，请检查 cookie.json 格式")

        log("[cookie] Cookie-Editor 导入完成")
    finally:
        popup.close()


def import_via_playwright(context: BrowserContext, cookies: list[dict[str, Any]]) -> None:
    playwright_cookies = cookie_editor_to_playwright(cookies)
    context.add_cookies(playwright_cookies)
    log(f"[cookie] 已通过 Playwright 写入 {len(playwright_cookies)} 个 cookie")


def import_cookies(
    context: BrowserContext,
    page: Page,
    cookie_file: Path,
    *,
    use_extension: bool = True,
) -> None:
    cookies = load_cookie_editor_json(cookie_file)
    cookie_json = json.dumps(cookies, ensure_ascii=False, indent=2)
    log(f"[cookie] 读取 {cookie_file}，共 {len(cookies)} 条")

    claude_domains = {c.get("domain", "") for c in cookies if "claude.ai" in (c.get("domain") or "")}
    if not claude_domains:
        raise ValueError("cookie.json 中未找到 claude.ai 相关 cookie")

    if use_extension:
        try:
            import_via_cookie_editor(context, cookie_json)
        except Exception as exc:
            log(f"[cookie] Cookie-Editor 导入失败，回退 Playwright API: {exc}")
            import_via_playwright(context, cookies)
    else:
        import_via_playwright(context, cookies)


def has_claude_session(context: BrowserContext) -> bool:
    names = {c.get("name") for c in context.cookies()}
    return "sessionKey" in names
