#!/usr/bin/env python3
"""openai_api 纯函数单测（不需要浏览器）。

运行：
    python scripts/test_openai_api.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import openai_api as OAI


def test_plain_chat_single_message():
    parsed = OAI.parse_messages({"messages": [{"role": "user", "content": "你好"}]})
    assert parsed.prompt == "你好"
    assert parsed.should_reset is True
    assert parsed.tool_mode is False


def test_plain_chat_multi_turn_takes_last_user():
    parsed = OAI.parse_messages(
        {
            "messages": [
                {"role": "user", "content": "第一句"},
                {"role": "assistant", "content": "回应"},
                {"role": "user", "content": "第二句"},
            ]
        }
    )
    assert parsed.prompt == "第二句"
    assert parsed.should_reset is False
    assert parsed.tool_mode is False


def test_content_parts_list():
    parsed = OAI.parse_messages(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "A"},
                        {"type": "text", "text": "B"},
                    ],
                }
            ]
        }
    )
    assert parsed.prompt == "A\nB"


def test_tool_mode_detected_and_serialized():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "List files in a directory",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]
    parsed = OAI.parse_messages(
        {
            "messages": [
                {"role": "system", "content": "You are an agent."},
                {"role": "user", "content": "当前目录有哪些文件"},
            ],
            "tools": tools,
        }
    )
    assert parsed.tool_mode is True
    assert parsed.should_reset is True
    assert "list_dir" in parsed.prompt
    assert "<tool_call>" in parsed.prompt
    assert "当前目录有哪些文件" in parsed.prompt
    assert "You are an agent." in parsed.prompt


def test_tool_choice_none_disables_tool_mode():
    parsed = OAI.parse_messages(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "x"}}],
            "tool_choice": "none",
        }
    )
    assert parsed.tool_mode is False


def test_serialize_includes_tool_results():
    tools = [{"type": "function", "function": {"name": "list_dir"}}]
    messages = [
        {"role": "user", "content": "列目录"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "list_dir", "arguments": '{"path": "."}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "list_dir", "content": "a.py\nb.py"},
    ]
    text = OAI.serialize_conversation(messages, OAI._normalize_tools(tools))
    assert "TOOL_RESULT" in text
    assert "a.py" in text
    assert '"name": "list_dir"' in text


def test_extract_single_tool_call():
    text = '<tool_call>{"name": "list_dir", "arguments": {"path": "."}}</tool_call>'
    calls = OAI.extract_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "list_dir"
    assert json.loads(calls[0]["arguments"]) == {"path": "."}


def test_extract_multiple_tool_calls():
    text = (
        '<tool_call>{"name": "read_file", "arguments": {"path": "a"}}</tool_call>\n'
        '<tool_call>{"name": "read_file", "arguments": {"path": "b"}}</tool_call>'
    )
    calls = OAI.extract_tool_calls(text)
    assert len(calls) == 2
    assert json.loads(calls[1]["arguments"])["path"] == "b"


def test_extract_tool_call_with_code_fence():
    text = "<tool_call>\n```json\n{\"name\": \"list_dir\", \"arguments\": {}}\n```\n</tool_call>"
    calls = OAI.extract_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "list_dir"


def test_extract_bare_json_fallback():
    text = '{"name": "list_dir", "arguments": {"path": "."}}'
    calls = OAI.extract_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "list_dir"


def test_extract_no_tool_calls_returns_empty():
    assert OAI.extract_tool_calls("当前目录下有 a.py 和 b.py。") == []


def test_strip_tool_calls():
    text = 'before<tool_call>{"name":"x","arguments":{}}</tool_call>after'
    assert OAI.strip_tool_calls(text) == "beforeafter"


def test_strip_thinking_prefix():
    text = "Thinking\nThinking for 0s\n\n当前目录有 a.py 和 b.py。"
    assert OAI.strip_thinking_prefix(text) == "当前目录有 a.py 和 b.py。"


def test_strip_thinking_prefix_keeps_plain_text():
    text = "当前目录有 a.py。\n第二行。"
    assert OAI.strip_thinking_prefix(text) == text


def test_tool_calls_payload_shape():
    payload = OAI.tool_calls_payload(
        "id1", "claude", [{"name": "list_dir", "arguments": "{}"}]
    )
    choice = payload["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    tc = choice["message"]["tool_calls"][0]
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "list_dir"
    assert tc["id"].startswith("call_")


def test_write_sse_tool_calls_emits_done():
    chunks: list[bytes] = []
    OAI.write_sse_tool_calls(
        "id1", "claude", [{"name": "list_dir", "arguments": "{}"}], chunks.append
    )
    blob = b"".join(chunks)
    assert b"tool_calls" in blob
    assert b'"finish_reason": "tool_calls"' in blob
    assert b"data: [DONE]" in blob


def _run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
