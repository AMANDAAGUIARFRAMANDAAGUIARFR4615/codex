#!/usr/bin/env python3
"""登录 claude.ai 后常驻一个 HTTP 服务，支持在会话期内多次提问、流式返回回答。

配合 frp（frpc.toml）把本机端口暴露到公网，你的控制台即可：

    curl -N "http://<frps_ip>:<remotePort>/ask?q=你的问题"

看到回答实时（流式）输出。会话默认 30 分钟，期间可多次提问（同一对话，带上下文）。

端点：
- GET  /ask?q=...    提问，流式返回回答（文本）
- POST /ask          请求体即问题，流式返回
- GET  /new          开启新对话（清空上下文）
- GET  /health       健康检查
- GET  /             用法说明
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# 辅助模块在 scripts/auth 与 scripts/common，加入搜索路径以保持扁平 import。
_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR / "auth"))
sys.path.insert(0, str(_SCRIPTS_DIR / "common"))

import login as L  # 复用登录流程（启动浏览器、导入 cookie、过 Cloudflare）
import claude_ask

USAGE = (
    "Claude 提问服务（会话期内可多次提问，流式返回）\n\n"
    "  GET  /ask?q=你的问题      流式返回回答\n"
    "  POST /ask  (body=问题)    流式返回回答\n"
    "  GET  /new                 开启新对话（清空上下文）\n"
    "  GET  /health              健康检查\n\n"
    '示例: curl -N "http://HOST:PORT/ask?q=用一句话介绍你自己"\n'
)


class AskServer(HTTPServer):
    def __init__(self, addr, handler, page, context, org):
        super().__init__(addr, handler)
        self.page = page
        self.context = context
        self.org = org
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0：响应结束即关闭连接，curl -N 能逐块看到流式输出。
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):  # noqa: A003
        L.log(f"[http] {self.address_string()} {fmt % args}")

    def _start(self, code=200, ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _text(self, code, body):
        self._start(code)
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self):
        route = urlparse(self.path)
        if route.path == "/health":
            self._text(200, "ok\n")
        elif route.path in ("/", "/help"):
            self._text(200, USAGE)
        elif route.path == "/new":
            self._new()
        elif route.path == "/ask":
            q = parse_qs(route.query).get("q", [""])[0]
            self._ask(q)
        else:
            self._text(404, "not found\n")

    def do_POST(self):
        route = urlparse(self.path)
        if route.path == "/ask":
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            self._ask(body)
        elif route.path == "/new":
            self._new()
        else:
            self._text(404, "not found\n")

    def _ask(self, question: str):
        question = (question or "").strip()
        if not question:
            self._text(400, '用法: /ask?q=你的问题  (或 POST 请求体填问题)\n')
            return

        self._start(200)

        def write(chunk: str):
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()

        L.log(f"[ask] 收到提问（{len(question)} 字）: {question[:60]}")
        with self.server.lock:
            try:
                full = claude_ask.stream_answer(
                    self.server.page, question, write, org_uuid=self.server.org
                )
                write("\n")
                L.log(f"[ask] 回答完成（{len(full)} 字）")
            except (BrokenPipeError, ConnectionResetError):
                L.log("[ask] 客户端已断开")
            except Exception as exc:  # noqa: BLE001
                L.log(f"[ask] 出错: {exc}")
                try:
                    write(f"\n[error] {exc}\n")
                except Exception:
                    pass

    def _new(self):
        with self.server.lock:
            try:
                self.server.page.goto(
                    L.CLAUDE_NEW_URL, wait_until="domcontentloaded", timeout=120000
                )
                self.server.page.wait_for_timeout(1500)
                L.wait_for_claude_ready(self.server.page, timeout=60)
                self._text(200, "已开始新对话\n")
            except Exception as exc:  # noqa: BLE001
                self._text(500, f"新对话失败: {exc}\n")


def run_server(page, context, org, port: int, minutes: int) -> None:
    server = AskServer(("127.0.0.1", port), Handler, page, context, org)
    L.log(f"[serve] HTTP 服务已启动: http://127.0.0.1:{port}（会话 {minutes} 分钟）")
    L.log("[serve] 端点: GET /ask?q=...  | POST /ask  | GET /new  | GET /health")

    def stop():
        L.log(f"[serve] 已运行 {minutes} 分钟，关闭服务。")
        server.shutdown()

    timer = threading.Timer(minutes * 60, stop)
    timer.daemon = True
    timer.start()
    try:
        server.serve_forever(poll_interval=1.0)
    finally:
        timer.cancel()
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="登录 claude.ai 并常驻提问服务")
    parser.add_argument(
        "cookie_file",
        nargs="?",
        default=os.environ.get("COOKIE_INPUT_FILE", "cookie.json"),
        help="Cookie-Editor JSON 文件路径（默认 cookie.json）",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("SERVE_PORT", "8787")),
        help="本机监听端口（frpc 转发到此端口，默认 8787）",
    )
    parser.add_argument(
        "--minutes", type=int, default=int(os.environ.get("SERVE_MINUTES", "30")),
        help="服务存活时长（分钟，默认 30）",
    )
    args = parser.parse_args()

    cookie_path = L.resolve_cookie_file(args.cookie_file)
    L.log(f"[serve] Cookie 文件: {cookie_path}")
    if L.get_extension_dir() is None:
        raise RuntimeError(
            "未加载 Cookie-Editor 扩展。请设置 LOAD_COOKIE_EXTENSION=true 和 COOKIE_EDITOR_DIR。"
        )

    L.capsolver_solver.log_account()

    with L.sync_playwright() as playwright:
        browser, context = L.launch_browser(playwright)
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(5000)
        try:
            L.open_claude(page)
            L.import_cookies(context, page, cookie_path, use_extension=True)
            L.reload_and_verify(context, page)

            page.goto(L.CLAUDE_NEW_URL, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(2000)
            L.wait_for_claude_ready(page, timeout=90)

            org = L.get_org_uuid(context)
            L.log(f"[serve] 登录完成，组织 UUID={org or '未知'}")
            run_server(page, context, org, args.port, args.minutes)
        except Exception:
            L.save_debug(page, "serve-error")
            raise
        finally:
            context.close()
            if browser is not None:
                browser.close()
            L.log("[browser] 浏览器已关闭")


if __name__ == "__main__":
    main()
