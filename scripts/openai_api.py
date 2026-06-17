"""OpenAI Chat Completions API 兼容层（供 serve.py 使用）。

除普通聊天外，这里还实现了「基于提示词的工具调用（function calling）shim」：
claude.ai 网页端本身不支持 OpenAI 的原生 function calling，但 Cursor / Cline 等
Agent 客户端依赖它（先让模型回 tool_calls，客户端本地执行工具，再把结果回传）。

做法：当请求里带 `tools` 时，把整段对话（system + 历史 + 工具结果）连同一段
协议说明序列化成纯文本发给 claude.ai，并约定模型用 `<tool_call>{...}</tool_call>`
的格式输出工具调用；拿到回答后再解析回 OpenAI 的 tool_calls 结构。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import namedtuple
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


# parse_messages 的返回结构。tool_mode=True 时 prompt 是整段对话的序列化文本，
# 且 should_reset 恒为 True（每轮都把完整上下文重发，不依赖 claude.ai 的会话记忆）。
ParsedRequest = namedtuple(
    "ParsedRequest", ["prompt", "should_reset", "model", "tool_mode"]
)


def new_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def new_tool_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"


def parse_messages(body: dict[str, Any]) -> ParsedRequest:
    """从 Chat Completions 请求体解析出要发送给 claude.ai 的文本。

    - 普通聊天：只取最后一条 user 消息（浏览器侧已保留上下文）；只有一条 user 消息
      时视为新会话（should_reset=True）。
    - Agent / 工具调用（请求带 `tools` 且 tool_choice 不为 "none"）：把整段对话序列化
      成文本（含工具协议说明），should_reset 恒为 True。
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages 不能为空")

    model = str(body.get("model") or DEFAULT_MODEL)
    tools = _normalize_tools(body.get("tools"))
    tool_choice = body.get("tool_choice")

    if tools and tool_choice != "none":
        prompt = serialize_conversation(messages, tools, tool_choice)
        if not prompt.strip():
            raise ValueError("无法从 messages 构造提示词")
        return ParsedRequest(prompt, True, model, True)

    user_messages = [
        msg for msg in messages if isinstance(msg, dict) and msg.get("role") == "user"
    ]
    if not user_messages:
        raise ValueError("messages 中缺少 role=user 的消息")

    prompt = _message_text(user_messages[-1].get("content"))
    if not prompt:
        raise ValueError("user 消息 content 为空")

    should_reset = len(user_messages) == 1
    return ParsedRequest(prompt, should_reset, model, False)


def _normalize_tools(tools: Any) -> list[dict[str, Any]]:
    """把 OpenAI tools（[{"type":"function","function":{...}}]）规整成函数定义列表。"""
    if not isinstance(tools, list):
        return []
    out: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if isinstance(fn, dict) and fn.get("name"):
            out.append(fn)
    return out


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


# ---------------------------------------------------------------------------
# 工具调用（function calling）shim
# ---------------------------------------------------------------------------

# 模型用来包裹工具调用的标记；解析时同样依赖它。
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_]*\s*|\s*```$")


def build_tool_preamble(tools: list[dict[str, Any]], tool_choice: Any = None) -> str:
    """工具调用协议说明（claude.ai 网页无原生 function calling，用最小提示词补齐）。

    仅描述「如何输出工具调用」这一格式约定，不再注入冗长的角色设定/沙箱说教——
    Agent 客户端（如 Cursor）自己的 system prompt 已包含任务说明，这里只补协议层。
    """
    lines = [
        "To act on the user's machine you MUST call the tools listed below. Output each tool call",
        "as RAW TEXT in EXACTLY this format — never inside code fences, artifacts, or markdown,",
        "and with nothing else around it:",
        '<tool_call>{"name": "<tool_name>", "arguments": {<json-args>}}</tool_call>',
        "",
        "- `arguments` must be valid JSON matching the tool's parameters.",
        "- Do NOT use any built-in code-execution, analysis, or artifact feature: that sandbox is",
        "  NOT the user's machine, so its results are wrong. Only the tools below act on the real project.",
        "- When calling tools, reply with only the <tool_call> block(s) and no other text.",
        "- After tool results come back, either call more tools or write the final plain-text answer.",
        "",
        "Available tools:",
    ]
    for fn in tools:
        name = fn.get("name", "")
        desc = (fn.get("description") or "").strip()
        params = fn.get("parameters") or {"type": "object", "properties": {}}
        lines.append(f"- {name}")
        if desc:
            lines.append(f"    description: {desc}")
        lines.append(f"    parameters: {json.dumps(params, ensure_ascii=False)}")

    forced = _forced_tool_name(tool_choice)
    if forced:
        lines += ["", f"You MUST call the tool `{forced}` now."]
    elif tool_choice == "required":
        lines += ["", "You MUST call at least one tool now."]
    return "\n".join(lines)


def _forced_tool_name(tool_choice: Any) -> str:
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            return str(fn["name"])
    return ""


def serialize_conversation(
    messages: list[Any], tools: list[dict[str, Any]], tool_choice: Any = None
) -> str:
    """把整段对话 + 工具协议序列化成一条发给 claude.ai 的文本。"""
    blocks = [build_tool_preamble(tools, tool_choice), "", "=== CONVERSATION ==="]

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            text = _message_text(msg.get("content"))
            if text:
                blocks.append(f"\n[SYSTEM]\n{text}")
        elif role == "user":
            text = _message_text(msg.get("content"))
            if text:
                blocks.append(f"\n[USER]\n{text}")
        elif role == "assistant":
            text = _message_text(msg.get("content"))
            if text:
                blocks.append(f"\n[ASSISTANT]\n{text}")
            for rendered in _render_assistant_tool_calls(msg.get("tool_calls")):
                blocks.append(f"\n[ASSISTANT_TOOL_CALL]\n{rendered}")
        elif role == "tool":
            name = msg.get("name") or ""
            call_id = msg.get("tool_call_id") or ""
            text = _message_text(msg.get("content"))
            header = "[TOOL_RESULT"
            if name:
                header += f" name={name}"
            if call_id:
                header += f" id={call_id}"
            header += "]"
            blocks.append(f"\n{header}\n{text}")

    blocks.append(
        "\n=== END CONVERSATION ===\n"
        "Now respond: emit <tool_call> block(s) to call tools, or — if the tool results already"
        " give you enough — write the final plain-text answer."
    )
    return "\n".join(blocks)


def _render_assistant_tool_calls(tool_calls: Any) -> list[str]:
    rendered: list[str] = []
    if not isinstance(tool_calls, list):
        return rendered
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = fn.get("name")
        if not name:
            continue
        raw_args = fn.get("arguments")
        try:
            args_obj = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except (TypeError, ValueError):
            args_obj = {}
        if not isinstance(args_obj, (dict, list)):
            args_obj = {}
        payload = {"name": name, "arguments": args_obj}
        rendered.append(f"<tool_call>{json.dumps(payload, ensure_ascii=False)}</tool_call>")
    return rendered


def extract_tool_calls(text: str) -> list[dict[str, str]]:
    """从模型回答里抽取工具调用，返回 [{"name", "arguments"(json str)}]。"""
    if not text:
        return []

    calls: list[dict[str, str]] = []
    for match in _TOOL_CALL_RE.finditer(text):
        parsed = _parse_tool_call_obj(match.group(1))
        if parsed:
            calls.append(parsed)

    # 兜底：没有标记，但整段回答本身就是 {"name":..., "arguments":...}。
    if not calls:
        stripped = _strip_code_fence(text.strip())
        if stripped.startswith("{") and '"name"' in stripped:
            parsed = _parse_tool_call_obj(stripped)
            if parsed:
                calls.append(parsed)
    return calls


def _parse_tool_call_obj(inner: str) -> dict[str, str] | None:
    obj = _loads_lenient(_strip_code_fence(inner.strip()))
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    if not name:
        return None
    args = obj.get("arguments", {})
    if isinstance(args, str):
        # 可能已经是 JSON 字符串；规整一下，无法解析则原样保留。
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return {"name": str(name), "arguments": args}
    if not isinstance(args, (dict, list)):
        args = {}
    return {"name": str(name), "arguments": json.dumps(args, ensure_ascii=False)}


def strip_tool_calls(text: str) -> str:
    """去掉文本里的 <tool_call> 块，返回剩余的普通文本。"""
    return _TOOL_CALL_RE.sub("", text or "").strip()


_THINKING_LINE_RE = re.compile(r"^(?:thinking|thought)(?:\s+for\s+[\d.]+\s*\w+)?$", re.IGNORECASE)
_THINKING_FOR_RE = re.compile(r"^for\s+[\d.]+\s*\w+$", re.IGNORECASE)


def strip_thinking_prefix(text: str) -> str:
    """去掉 claude.ai DOM 抓取里开头的「Thinking / Thinking for Ns」思考链 UI 文本。

    仅作 DOM 兜底用；优先来源（内部 API 文本）本就不含思考块。
    """
    lines = (text or "").split("\n")
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == "" or _THINKING_LINE_RE.match(stripped) or _THINKING_FOR_RE.match(stripped):
            i += 1
            continue
        break
    return "\n".join(lines[i:]).strip()


def _strip_code_fence(text: str) -> str:
    if text.startswith("```"):
        text = _CODE_FENCE_RE.sub("", text)
        text = _CODE_FENCE_RE.sub("", text)
    return text.strip()


def _loads_lenient(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# 响应载荷（普通 + 工具调用）
# ---------------------------------------------------------------------------


def models_payload() -> dict[str, Any]:
    return {"object": "list", "data": MODELS}


def _usage() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


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
        "usage": _usage(),
    }


def tool_calls_payload(
    completion_id: str, model: str, calls: list[dict[str, str]]
) -> dict[str, Any]:
    tool_calls = [
        {
            "id": new_tool_call_id(),
            "type": "function",
            "function": {"name": c["name"], "arguments": c["arguments"]},
        }
        for c in calls
    ]
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": _usage(),
    }


def error_payload(message: str, *, err_type: str = "invalid_request_error", code: str | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"message": message, "type": err_type}
    if code:
        err["code"] = code
    return {"error": err}


# ---------------------------------------------------------------------------
# SSE 流式输出
# ---------------------------------------------------------------------------


def sse_line(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def sse_done() -> bytes:
    return b"data: [DONE]\n\n"


def sse_comment(text: str = "keep-alive") -> bytes:
    """SSE 注释行（以 ':' 开头），客户端会忽略，用作心跳防止连接被中间层断开。"""
    return f": {text}\n\n".encode("utf-8")


def _chunk(completion_id: str, model: str, delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def sse_role(completion_id: str, model: str) -> bytes:
    return sse_line(_chunk(completion_id, model, {"role": "assistant"}))


def sse_content(completion_id: str, model: str, text: str) -> bytes:
    return sse_line(_chunk(completion_id, model, {"content": text}))


def sse_finish(completion_id: str, model: str, reason: str = "stop") -> bytes:
    return sse_line(_chunk(completion_id, model, {}, finish=reason))


def sse_tool_calls_delta(completion_id: str, model: str, calls: list[dict[str, str]]) -> list[bytes]:
    out: list[bytes] = []
    for index, call in enumerate(calls):
        out.append(
            sse_line(
                _chunk(
                    completion_id,
                    model,
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": new_tool_call_id(),
                                "type": "function",
                                "function": {
                                    "name": call["name"],
                                    "arguments": call["arguments"],
                                },
                            }
                        ]
                    },
                )
            )
        )
    return out


def make_sse_delta_writer(
    completion_id: str,
    model: str,
    on_write: Callable[[bytes], None],
    emit_role: bool = True,
) -> Callable[[str], None]:
    """返回传给 stream_answer 的 on_delta：把纯文本增量包装成 OpenAI SSE chunk。"""
    if emit_role:
        on_write(sse_role(completion_id, model))

    def emit(text: str) -> None:
        if text:
            on_write(sse_content(completion_id, model, text))

    return emit


def write_sse_finish(completion_id: str, model: str, on_write: Callable[[bytes], None]) -> None:
    on_write(sse_finish(completion_id, model, "stop"))
    on_write(sse_done())


def write_sse_tool_calls(
    completion_id: str,
    model: str,
    calls: list[dict[str, str]],
    on_write: Callable[[bytes], None],
    emit_role: bool = True,
) -> None:
    """把工具调用以 OpenAI 流式格式（delta.tool_calls）发出，并以 tool_calls 结束。"""
    if emit_role:
        on_write(sse_role(completion_id, model))
    for chunk in sse_tool_calls_delta(completion_id, model, calls):
        on_write(chunk)
    on_write(sse_finish(completion_id, model, "tool_calls"))
    on_write(sse_done())
