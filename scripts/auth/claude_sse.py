"""解析 claude.ai completion 接口的原始 SSE 流（纯函数，无第三方依赖，便于单测）。

兼容两种格式：
- Anthropic Messages 流式：``content_block_start`` / ``content_block_delta``
  （``text_delta`` 进正文、``thinking_delta`` 进思考、``input_json_delta`` 忽略）、
  ``message_delta`` 带 ``stop_reason``。
- 旧版 claude.ai：``{"type":"completion","completion":"..."}``（completion 为增量）。
"""

from __future__ import annotations

import json


def parse_completion_sse(raw: str) -> tuple[str, str, str]:
    """解析原始 SSE 文本，返回 (正文, 思考, stop_reason)。"""
    if not raw:
        return "", "", ""

    text_parts: list[str] = []
    think_parts: list[str] = []
    stop_reason = ""

    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue

        typ = obj.get("type")
        comp = obj.get("completion")
        if isinstance(comp, str):  # 旧格式：completion 为增量
            text_parts.append(comp)

        if typ == "content_block_start":
            cb = obj.get("content_block") or {}
            if isinstance(cb, dict) and isinstance(cb.get("text"), str):
                text_parts.append(cb["text"])
        elif typ == "content_block_delta":
            delta = obj.get("delta") or {}
            dtyp = delta.get("type")
            if dtyp == "text_delta" and isinstance(delta.get("text"), str):
                text_parts.append(delta["text"])
            elif dtyp == "thinking_delta" and isinstance(delta.get("thinking"), str):
                think_parts.append(delta["thinking"])
        elif typ == "message_delta":
            sr = (obj.get("delta") or {}).get("stop_reason")
            if sr:
                stop_reason = sr

        if obj.get("stop_reason"):
            stop_reason = obj["stop_reason"]

    return "".join(text_parts), "".join(think_parts), stop_reason
