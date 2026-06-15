"""将 Playwright cookies 导出为 Cookie-Editor JSON 格式。"""

from __future__ import annotations

import json
import time
from typing import Any


SAMESITE_MAP = {
    "Strict": "strict",
    "Lax": "lax",
    "None": "no_restriction",
}


def to_cookie_editor_format(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    exported: list[dict[str, Any]] = []

    for cookie in cookies:
        domain = cookie.get("domain", "")
        host_only = not domain.startswith(".")
        expires = cookie.get("expires", -1)
        session = expires in (-1, None)

        item = {
            "domain": domain,
            "hostOnly": host_only,
            "httpOnly": bool(cookie.get("httpOnly", False)),
            "name": cookie.get("name", ""),
            "path": cookie.get("path", "/"),
            "sameSite": SAMESITE_MAP.get(cookie.get("sameSite"), "unspecified"),
            "secure": bool(cookie.get("secure", False)),
            "session": session,
            "storeId": "0",
            "value": cookie.get("value", ""),
        }

        if not session and isinstance(expires, (int, float)) and expires > 0:
            item["expirationDate"] = int(expires)

        exported.append(item)

    return exported


def print_cookie_editor_export(cookies: list[dict[str, Any]], domain_filter: str = "cursor") -> str:
    filtered = [
        c
        for c in cookies
        if domain_filter in (c.get("domain") or "") or domain_filter in (c.get("name") or "")
    ]
    payload = to_cookie_editor_format(filtered or cookies)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print("\n========== Cookie-Editor JSON Export ==========")
    print(text)
    print("===============================================\n")
    return text
