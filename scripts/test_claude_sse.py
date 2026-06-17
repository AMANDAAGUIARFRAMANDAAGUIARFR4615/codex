#!/usr/bin/env python3
"""claude_sse 纯函数单测（不需要浏览器/playwright）。

运行：
    python3 scripts/test_claude_sse.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "auth"))

from claude_sse import parse_completion_sse


def test_anthropic_text_stream():
    raw = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"content":[]}}\n\n'
        'event: content_block_start\n'
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}\n\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" world"}}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    )
    text, thinking, stop = parse_completion_sse(raw)
    assert text == "Hello world", repr(text)
    assert thinking == ""
    assert stop == "end_turn"


def test_thinking_excluded_from_text():
    raw = (
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking"}}\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"let me think"}}\n\n'
        'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n\n'
        'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"answer"}}\n\n'
    )
    text, thinking, _ = parse_completion_sse(raw)
    assert text == "answer", repr(text)
    assert thinking == "let me think", repr(thinking)


def test_tool_call_text_preserved():
    raw = (
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"<tool_call>{\\"name\\":"}}\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" \\"list_dir\\", \\"arguments\\": {}}</tool_call>"}}\n\n'
    )
    text, _, _ = parse_completion_sse(raw)
    assert text == '<tool_call>{"name": "list_dir", "arguments": {}}</tool_call>', repr(text)


def test_legacy_completion_format():
    raw = (
        'event: completion\n'
        'data: {"type":"completion","completion":"part1","stop_reason":null}\n\n'
        'event: completion\n'
        'data: {"type":"completion","completion":" part2","stop_reason":"stop_sequence"}\n\n'
    )
    text, _, stop = parse_completion_sse(raw)
    assert text == "part1 part2", repr(text)
    assert stop == "stop_sequence"


def test_ping_and_garbage_ignored():
    raw = (
        'event: ping\n'
        'data: {"type":"ping"}\n\n'
        ': this is an SSE comment / heartbeat\n\n'
        'data: not-json\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n'
    )
    text, _, _ = parse_completion_sse(raw)
    assert text == "ok", repr(text)


def test_empty_input():
    assert parse_completion_sse("") == ("", "", "")
    assert parse_completion_sse(None) == ("", "", "")  # type: ignore[arg-type]


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
