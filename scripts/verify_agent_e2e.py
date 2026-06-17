#!/usr/bin/env python3
"""端到端验证 Agent / 工具调用是否生效（连真实的 frp 公网服务）。

用法:
    python3 scripts/verify_agent_e2e.py [base_url]

默认 base_url 取自 frpc.toml（http://8.210.199.147:6000/v1）。
依次：等待 /health 就绪 -> 发一个带 tools 的请求 -> 检查是否返回 tool_calls。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "http://8.210.199.147:6000/v1"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories under the given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path, '.' for current."}
                },
                "required": ["path"],
            },
        },
    }
]


def _post(url: str, payload: dict, timeout: int = 240) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer sk-local"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_health(base: str, timeout: int = 360) -> bool:
    health = base.replace("/v1", "") + "/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health, timeout=10) as resp:
                if resp.status == 200:
                    print(f"[ok] health 就绪: {health}")
                    return True
        except Exception as exc:  # noqa: BLE001
            print(f"[..] 等待服务就绪... ({exc})")
        time.sleep(10)
    return False


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE
    if not wait_health(base):
        print("[fail] 服务未在限定时间内就绪")
        return 1

    payload = {
        "model": "claude",
        "stream": False,
        "tools": TOOLS,
        "messages": [
            {"role": "system", "content": "You are a coding agent with filesystem tools."},
            {"role": "user", "content": "当前目录有哪些文件？请用工具查看。"},
        ],
    }
    print("[..] 发送带 tools 的请求...")
    try:
        resp = _post(base + "/chat/completions", payload)
    except urllib.error.HTTPError as exc:
        print(f"[fail] HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}")
        return 1

    print(json.dumps(resp, ensure_ascii=False, indent=2))
    choice = (resp.get("choices") or [{}])[0]
    finish = choice.get("finish_reason")
    tool_calls = (choice.get("message") or {}).get("tool_calls")
    if finish == "tool_calls" and tool_calls:
        print(f"\n[PASS] 模型返回了 tool_calls：{[t['function']['name'] for t in tool_calls]}")
        return 0
    print("\n[WARN] 未返回 tool_calls（模型可能直接回了文本）。content:")
    print((choice.get("message") or {}).get("content"))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
