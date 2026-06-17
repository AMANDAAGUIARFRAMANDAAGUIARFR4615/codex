#!/usr/bin/env python3
"""复现 Cursor agent 调用：流式 turn1（工具调用）+ turn2（基于结果的最终回答），打印时序。"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://8.210.199.147:6000/v1"

SYSTEM = (
    "You are a powerful agentic AI coding assistant operating in Cursor. "
    "You pair-program with the user to solve coding tasks. You have tools to read files, "
    "list directories, run terminal commands, and edit code. Always prefer using tools to "
    "gather real information rather than guessing. " * 6
)

TOOLS = [
    {"type": "function", "function": {"name": "list_dir", "description": "List a directory.",
     "parameters": {"type": "object", "properties": {"relative_workspace_path": {"type": "string"}}, "required": ["relative_workspace_path"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a file.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "run_terminal_cmd", "description": "Run a shell command.",
     "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "grep_search", "description": "Regex search.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
]

LS_OUTPUT = (
    "total 80\ndrwxr-xr-x  14 lin staff   448 Jun 17 18:00 .\ndrwxr-xr-x  20 lin staff   640 Jun 17 17:00 ..\n"
    "drwxr-xr-x  12 lin staff   384 Jun 17 18:00 .git\n-rw-r--r--   1 lin staff  6543 Jun 17 18:00 README.md\n"
    "-rw-r--r--   1 lin staff   612 Jun 17 18:00 frpc.toml\n-rw-r--r--   1 lin staff  4096 Jun 17 18:00 cookie.json\n"
    "drwxr-xr-x  16 lin staff   512 Jun 17 18:00 scripts\ndrwxr-xr-x   3 lin staff    96 Jun 17 18:00 .github\n"
)


def stream(payload: dict, label: str) -> str:
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer sk-local"},
    )
    t0 = time.time()
    first = None
    collected = []
    print(f"\n=== {label} (stream) ===")
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if first is None:
                first = time.time() - t0
                print(f"[time-to-first-line] {first:.1f}s")
            if line.startswith("data:"):
                data = line[5:].strip()
                if data == "[DONE]":
                    print(f"[DONE] total={time.time()-t0:.1f}s")
                    break
                collected.append(data)
    print(f"chunks={len(collected)}")
    return "\n".join(collected)


def main() -> None:
    base_msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "当前目录有哪些文件"},
    ]
    out1 = stream({"model": "claude", "stream": True, "tools": TOOLS, "messages": base_msgs}, "turn1")
    print(out1[-1] if out1 else "", "...")

    turn2 = base_msgs + [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "run_terminal_cmd", "arguments": json.dumps({"command": "ls -la"})}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "run_terminal_cmd", "content": LS_OUTPUT},
    ]
    stream({"model": "claude", "stream": True, "tools": TOOLS, "messages": turn2}, "turn2")


if __name__ == "__main__":
    main()
