#!/usr/bin/env python3
"""登录 claude.ai 后常驻 OpenAI 兼容 API，配合 frp 暴露到公网。

客户端把 Base URL 设为 http://<frps_ip>:<remotePort>/v1 即可（ChatBox、
OpenAI SDK、Cursor 自定义模型等）。默认流式 SSE，会话 30 分钟内可多次提问。

端点：
- POST /v1/chat/completions   Chat Completions（stream 默认 true 时 SSE）
- GET  /v1/models               模型列表
- GET  /health                  健康检查
- GET  /                        用法说明
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR / "auth"))
sys.path.insert(0, str(_SCRIPTS_DIR / "common"))
sys.path.insert(0, str(_SCRIPTS_DIR))

import claude_ask
import login as L
import openai_api as OAI

USAGE = (
    "Claude OpenAI 兼容 API（会话期内可多次提问，默认流式 SSE）\n\n"
    "  POST /v1/chat/completions\n"
    "  GET  /v1/models\n"
    "  GET  /health\n\n"
    "客户端 Base URL: http://HOST:PORT/v1\n"
    "模型名: claude\n"
    "API Key: 任意非空字符串（若设置了 SERVE_API_KEY 则需匹配）\n\n"
    "示例 (OpenAI Python SDK):\n"
    "  from openai import OpenAI\n"
    "  client = OpenAI(base_url='http://HOST:PORT/v1', api_key='sk-local')\n"
    "  stream = client.chat.completions.create(\n"
    "      model='claude', messages=[{'role':'user','content':'你好'}], stream=True)\n"
    "  for chunk in stream:\n"
    "      print(chunk.choices[0].delta.content or '', end='', flush=True)\n"
)


class AskServer(HTTPServer):
    def __init__(self, addr, handler, page, context, org, api_key: str = ""):
        super().__init__(addr, handler)
        self.page = page
        self.context = context
        self.org = org
        self.api_key = api_key
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        L.log(f"[http] {self.address_string()} {fmt % args}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, body: str):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else "{}"
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}

    def _check_auth(self) -> bool:
        required = self.server.api_key
        if not required:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {required}":
            return True
        self._json(401, OAI.error_payload("Invalid API Key", err_type="authentication_error", code="invalid_api_key"))
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        route = urlparse(self.path)
        if route.path == "/health":
            self._text(200, "ok\n")
        elif route.path in ("/", "/help"):
            self._text(200, USAGE)
        elif route.path == "/v1/models":
            if not self._check_auth():
                return
            self._json(200, OAI.models_payload())
        else:
            self._json(404, OAI.error_payload("Not found", err_type="invalid_request_error", code="not_found"))

    def do_POST(self):
        route = urlparse(self.path)
        if route.path == "/v1/chat/completions":
            if not self._check_auth():
                return
            self._chat_completions()
        else:
            self._json(404, OAI.error_payload("Not found", err_type="invalid_request_error", code="not_found"))

    def _chat_completions(self):
        try:
            body = self._read_json()
            prompt, should_reset, model = OAI.parse_messages(body)
        except json.JSONDecodeError:
            self._json(400, OAI.error_payload("Invalid JSON body"))
            return
        except ValueError as exc:
            self._json(400, OAI.error_payload(str(exc)))
            return

        stream = body.get("stream", True)
        if isinstance(stream, str):
            stream = stream.lower() not in ("false", "0", "no")

        completion_id = OAI.new_completion_id()
        L.log(f"[openai] model={model} stream={stream} reset={should_reset} prompt={prompt[:60]!r}")

        with self.server.lock:
            try:
                if should_reset:
                    self._reset_conversation()
                if stream:
                    self._stream_completion(completion_id, model, prompt)
                else:
                    self._blocking_completion(completion_id, model, prompt)
            except (BrokenPipeError, ConnectionResetError):
                L.log("[openai] 客户端已断开")
            except Exception as exc:  # noqa: BLE001
                L.log(f"[openai] 出错: {exc}")
                try:
                    self._json(500, OAI.error_payload(str(exc), err_type="server_error"))
                except Exception:
                    pass

    def _reset_conversation(self) -> None:
        L.log("[openai] 开启新对话（客户端仅一条 user 消息）")
        self.server.page.goto(L.CLAUDE_NEW_URL, wait_until="domcontentloaded", timeout=120000)
        self.server.page.wait_for_timeout(1500)
        L.wait_for_claude_ready(self.server.page, timeout=60)

    def _stream_completion(self, completion_id: str, model: str, prompt: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()

        def write(data: bytes):
            self.wfile.write(data)
            self.wfile.flush()

        emit = OAI.make_sse_delta_writer(completion_id, model, write)
        full = claude_ask.stream_answer(self.server.page, prompt, emit, org_uuid=self.server.org)
        OAI.write_sse_finish(completion_id, model, write)
        L.log(f"[openai] 流式完成（{len(full)} 字）")

    def _blocking_completion(self, completion_id: str, model: str, prompt: str) -> None:
        full = claude_ask.stream_answer(
            self.server.page,
            prompt,
            lambda _part: None,
            org_uuid=self.server.org,
        )
        L.log(f"[openai] 非流式完成（{len(full)} 字）")
        self._json(200, OAI.completion_payload(completion_id, model, full))


def run_server(page, context, org, port: int, minutes: int, api_key: str = "") -> None:
    server = AskServer(("127.0.0.1", port), Handler, page, context, org, api_key)
    L.log(f"[serve] OpenAI API: http://127.0.0.1:{port}/v1（会话 {minutes} 分钟）")
    L.log("[serve] POST /v1/chat/completions  | GET /v1/models  | GET /health")
    if api_key:
        L.log("[serve] 已启用 SERVE_API_KEY 鉴权")

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
    parser = argparse.ArgumentParser(description="登录 claude.ai 并常驻 OpenAI 兼容 API")
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
    api_key = os.environ.get("SERVE_API_KEY", "").strip()

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
            run_server(page, context, org, args.port, args.minutes, api_key)
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
