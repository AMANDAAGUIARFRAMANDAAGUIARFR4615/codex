"""从星辰邮箱大师 (xckj.site) 获取 Cursor 登录验证码。

只接受“点击获取验证码之后”到达的邮件：
调用方先用 ``latest_cursor_date()`` 记录基线时间，点击发码后再调
``wait_for_code(after=baseline)``，仅返回 ``date`` 严格晚于基线的 Cursor 邮件验证码，
避免把收件箱里残留的旧验证码误当成本次的码。

真实接口（从前端页面 fetchEmails / showDetail 反推）：
- 列表: ``GET /easy-mailbox/emails?email=&password=&mailbox=inbox|junk&limit=``
        返回数组，每项含 ``id`` / ``subject`` / ``from`` / ``date``（ISO-8601 UTC，最新在前）。
- 详情: ``GET /easy-mailbox/email_detail?email=&password=&mailbox=&id=``
        返回 ``{date, from, html, subject, text}``，``text`` 里含“one-time code is: 563589”。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import requests

from io_utils import setup_utf8_stdio

setup_utf8_stdio()

MAILBOX_BASE = "https://www.xckj.site/easy-mailbox"

# Cursor 验证码邮件特征：发件人（no-reply@cursor.sh）或登录主题。
# 注意：stripe 的“payment to Cursor”账单邮件主题里也含 cursor，必须排除，
# 否则会去拉取它们的详情（接口很慢）且白费一轮。
CURSOR_FROM_MARKERS = ("cursor.sh", "cursor.com")
CURSOR_SUBJECT_MARKERS = (
    "sign in to cursor",
    "登录 cursor",
    "sign-in code",
    "verification code",
    "验证码",
    "one-time code",
)

# “Your one-time code is: 563589” / “一次性验证码：563589” —— 优先按提示词就近取 6 位码
_CODE_AFTER_HINT = re.compile(
    r"(?:one[\s-]*time code|verification code|sign[\s-]*in code|code is|验证码|一次性)[^\d]{0,40}(\d{6})",
    re.IGNORECASE,
)
_CODE_FALLBACK = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def _parse_date(value: str) -> Optional[datetime]:
    """把邮件 date 字段解析成带时区的 UTC datetime；失败返回 None。"""
    if not value:
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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

    # ---- 原始接口 ---------------------------------------------------------
    def list_emails(self, mailbox: str = "inbox") -> list[dict]:
        params = {
            "email": self.email,
            "password": self.password,
            "mailbox": mailbox,
            "limit": self.limit,
        }
        try:
            resp = self.session.get(f"{MAILBOX_BASE}/emails", params=params, timeout=30)
            if resp.status_code != 200:
                return []
            data = resp.json()
        except (requests.RequestException, ValueError):
            return []
        return data if isinstance(data, list) else []

    def fetch_detail(self, mailbox: str, mail_id) -> Optional[dict]:
        params = {
            "email": self.email,
            "password": self.password,
            "mailbox": mailbox,
            "id": mail_id,
        }
        try:
            resp = self.session.get(
                f"{MAILBOX_BASE}/email_detail", params=params, timeout=30
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
        except (requests.RequestException, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # ---- Cursor 验证码逻辑 ------------------------------------------------
    @staticmethod
    def _is_cursor_mail(item: dict) -> bool:
        """是否为 Cursor 登录验证码邮件（排除 stripe 账单等同样含 cursor 的邮件）。"""
        frm = (item.get("from") or "").lower()
        subj = (item.get("subject") or "").lower()
        if any(marker in frm for marker in CURSOR_FROM_MARKERS):
            return True
        return any(marker in subj for marker in CURSOR_SUBJECT_MARKERS)

    def _extract_code(self, text: str) -> Optional[str]:
        if not text:
            return None
        hinted = _CODE_AFTER_HINT.search(text)
        if hinted:
            code = hinted.group(1)
            if code not in self.email:
                return code
        for match in _CODE_FALLBACK.finditer(text):
            code = match.group(1)
            if code in self.email:
                continue
            return code
        return None

    def latest_cursor_date(self) -> Optional[datetime]:
        """收件箱/垃圾箱里最新一封 Cursor 邮件的时间，作为“点击发码前”的基线。"""
        latest: Optional[datetime] = None
        for mailbox in ("inbox", "junk"):
            for item in self.list_emails(mailbox):
                if not self._is_cursor_mail(item):
                    continue
                dt = _parse_date(item.get("date", ""))
                if dt and (latest is None or dt > latest):
                    latest = dt
        return latest

    def fetch_code_once(self, after: Optional[datetime] = None) -> Optional[str]:
        """扫描收件箱/垃圾箱，返回第一封满足时间条件的 Cursor 邮件中的验证码。"""
        for mailbox in ("inbox", "junk"):
            for item in self.list_emails(mailbox):
                if not self._is_cursor_mail(item):
                    continue
                dt = _parse_date(item.get("date", ""))
                # 必须晚于基线：点击发码之前的旧邮件一律忽略
                if after is not None and (dt is None or dt <= after):
                    continue
                detail = self.fetch_detail(mailbox, item.get("id"))
                source = ""
                if detail:
                    source = f"{detail.get('text', '')}\n{detail.get('subject', '')}"
                code = self._extract_code(source) or self._extract_code(
                    item.get("subject", "")
                )
                if code:
                    print(
                        f"[mailbox] 命中邮件 id={item.get('id')} mailbox={mailbox} "
                        f"date={item.get('date')} from={item.get('from')} -> {code}"
                    )
                    return code
        return None

    def wait_for_code(
        self,
        timeout: int = 180,
        interval: int = 5,
        page=None,
        after: Optional[datetime] = None,
    ) -> str:
        deadline = time.time() + timeout
        attempt = 0
        frontend_opened = False

        if after is not None:
            print(f"[mailbox] 仅接受晚于基线 {after.isoformat()} 的 Cursor 邮件")
        else:
            print("[mailbox] 未设置基线，接受最新一封 Cursor 邮件（建议传 after 以保证是本次的码）")

        # 用本机 Chrome 打开前端页面，便于可视化核验（仅展示，取码仍以 API 为准）。
        if page is not None:
            try:
                page.goto(self.frontend_url(), wait_until="domcontentloaded", timeout=120000)
                frontend_opened = True
                print(f"[mailbox] 已在浏览器打开邮箱前端: {self.frontend_url()}")
            except Exception as exc:  # noqa: BLE001
                print(f"[mailbox] 打开邮箱前端失败（不影响 API 取码）: {exc}")

        while time.time() < deadline:
            attempt += 1
            print(f"[mailbox] 第 {attempt} 次查询验证码...")

            code = self.fetch_code_once(after)
            if code:
                print(f"[mailbox] 获取到验证码: {code}")
                return code

            if frontend_opened:
                try:
                    page.reload(wait_until="domcontentloaded")
                except Exception:  # noqa: BLE001
                    pass

            time.sleep(interval)

        raise TimeoutError(
            f"在 {timeout} 秒内未从邮箱 {self.email} 获取到“点击发码之后”的 Cursor 验证码。"
            f" 可手动打开: {self.frontend_url()}"
        )
