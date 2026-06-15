"""从星辰邮箱大师 (xckj.site) 获取 Cursor 验证码。"""

from __future__ import annotations

import re
import time
from typing import Optional
from urllib.parse import quote

import requests

from io_utils import setup_utf8_stdio

setup_utf8_stdio()

MAILBOX_BASE = "https://www.xckj.site/easy-mailbox"
CODE_PATTERN = re.compile(r"\b(\d{6})\b")


class MailboxClient:
    def __init__(self, email: str, password: str, limit: int = 15):
        self.email = email
        self.password = password
        self.limit = limit
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
            }
        )

    def frontend_url(self) -> str:
        return (
            f"{MAILBOX_BASE}/frontend"
            f"?email={quote(self.email)}&password={quote(self.password)}"
        )

    def _extract_code(self, text: str) -> Optional[str]:
        if not text:
            return None
        for match in CODE_PATTERN.finditer(text):
            code = match.group(1)
            if code in self.email:
                continue
            return code
        return None

    def _parse_response(self, data) -> Optional[str]:
        if isinstance(data, dict):
            for key in ("verification_code", "code", "verify_code"):
                value = data.get(key)
                if isinstance(value, str) and CODE_PATTERN.fullmatch(value):
                    return value

            for key in ("text", "body", "content", "html", "subject"):
                code = self._extract_code(str(data.get(key, "")))
                if code:
                    return code

            mails = data.get("mails") or data.get("emails") or data.get("data") or []
            if isinstance(mails, list):
                for mail in mails:
                    if isinstance(mail, dict):
                        code = self._parse_response(mail)
                        if code:
                            return code
                    elif isinstance(mail, str):
                        code = self._extract_code(mail)
                        if code:
                            return code

        if isinstance(data, list):
            for item in data:
                code = self._parse_response(item)
                if code:
                    return code

        if isinstance(data, str):
            return self._extract_code(data)

        return None

    def _request_mail_api(self, path: str, method: str = "GET") -> Optional[str]:
        params = {
            "email": self.email,
            "password": self.password,
            "mailbox": "INBOX",
            "limit": self.limit,
        }
        url = f"{MAILBOX_BASE}{path}"

        try:
            if method == "POST":
                response = self.session.post(url, json=params, timeout=20)
            else:
                response = self.session.get(url, params=params, timeout=20)

            if response.status_code != 200:
                return None

            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return self._extract_code(response.text)

            return self._parse_response(response.json())
        except requests.RequestException:
            return None

    def fetch_code_once(self) -> Optional[str]:
        endpoints = [
            ("/api/mail-new", "GET"),
            ("/api/mail-new", "POST"),
            ("/api/mail-all", "GET"),
            ("/api/fetch-mails", "POST"),
            ("/api/mails", "GET"),
        ]
        for path, method in endpoints:
            code = self._request_mail_api(path, method)
            if code:
                return code
        return None

    def wait_for_code(
        self,
        timeout: int = 180,
        interval: int = 8,
        page=None,
    ) -> str:
        deadline = time.time() + timeout
        attempt = 0
        frontend_ready = False

        while time.time() < deadline:
            attempt += 1
            print(f"[mailbox] 第 {attempt} 次查询验证码...")

            code = self.fetch_code_once()
            if code:
                print(f"[mailbox] 获取到验证码: {code}")
                return code

            if page is not None:
                if not frontend_ready:
                    page.goto(self.frontend_url(), wait_until="domcontentloaded", timeout=120000)
                    frontend_ready = True
                else:
                    page.reload(wait_until="domcontentloaded")

                page_text = page.locator("body").inner_text()
                code = self._extract_code(page_text)
                if code:
                    print(f"[mailbox] 从前端页面获取到验证码: {code}")
                    return code

            time.sleep(interval)

        raise TimeoutError(
            f"在 {timeout} 秒内未从邮箱 {self.email} 获取到 Cursor 验证码。"
            f" 可手动打开: {self.frontend_url()}"
        )
