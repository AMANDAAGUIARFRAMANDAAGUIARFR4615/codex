"""OpenAI Chat Completions API 兼容层（供 serve.py 使用）。"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable


DEFAULT_MODEL = "claude"
MODELS = [
    {
        "id": DEFAULT_MODEL,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "claude.ai",
    }
]


def new_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def parse_messages(body: dict[str, Any]) -> tuple[str, bool, str]:
    """从 Chat Completions 请求体提取要发送的文本与是否应开启新对话。

    返回 (prompt, should_reset, model)。
    - 只取最后一条 user 消息发给浏览器（浏览器侧已保留上下文）。
    - 若 messages 里只有一条 user 消息（system 不计），视为客户端新会话，先 /new。
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages 不能为空")

    user_messages: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        user_messages.append(msg)

    if not user_messages:
        raise ValueError("messages 中缺少 role=user 的消息")

    last = user_messages[-1]
    prompt = _message_text(last.get("content"))
    if not prompt:
        raise ValueError("user 消息 content 为空")

    should_reset = len(user_messages) == 1
    model = str(body.get("model") or DEFAULT_MODEL)
    return prompt, should_reset, model


def _message_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("text", "input_text") and part.get("text"):
                parts.append(str(part["text"]))
        return "\n".join(parts).strip()
    return str(content).strip()


def models_payload() -> dict[str, Any]:
    return {"object": "list", "data": MODELS}


def completion_payload(completion_id: str, model: str, content: str) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def error_payload(message: str, *, err_type: str = "invalid_request_error", code: str | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"message": message, "type": err_type}
    if code:
        err["code"] = code
    return {"error": err}


def sse_line(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def sse_done() -> bytes:
    return b"data: [DONE]\n\n"


def make_sse_delta_writer(
    completion_id: str,
    model: str,
    on_write: Callable[[bytes], None],
) -> Callable[[str], None]:
    """返回传给 stream_answer 的 on_delta：把纯文本增量包装成 OpenAI SSE chunk。"""
    created = int(time.time())
    role_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    on_write(sse_line(role_chunk))

    def emit(text: str) -> None:
        if not text:
            return
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        }
        on_write(sse_line(chunk))

    return emit


def write_sse_finish(completion_id: str, model: str, on_write: Callable[[bytes], None]) -> None:
    finish = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    on_write(sse_line(finish))
    on_write(sse_done())
