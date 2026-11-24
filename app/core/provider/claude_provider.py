"""Claude（Anthropic Messages API）适配器。

与 OpenAI 兼容接口的差异都在这里吸收：

- ``system`` 是独立参数，不是消息；
- 工具 schema 叫 ``input_schema``；
- 工具结果要作为 **user 消息里的 ``tool_result`` 块**回传，且可以多条合并成一条；
- 工具调用是响应里的 ``tool_use`` 块，``input`` 是对象（需要重新序列化成 JSON 文本）。

前缀契约不变：``prefix`` 里的 system 消息拼成 ``system`` 参数，其余前缀消息按顺序
放在历史最前面。
"""

import json
from typing import Any

from app.common.api.llm_provider import (
    CompletionRequest,
    ModelResponse,
    ModelToolCall,
)
from app.common.exception import ProviderError

DEFAULT_MODEL = "claude-sonnet-4-5"
"""兜底模型；``config.yaml`` 的 ``llm.providers.<name>.model`` 可覆盖。"""

DEFAULT_MAX_TOKENS = 4096
"""Anthropic 必须显式给 ``max_tokens``，这里是安全默认值。"""


def _parse_arguments(arguments: str) -> dict[str, Any]:
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return {"_raw": arguments}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


def _tool_result_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI 风格的 ``role=tool`` 消息 → Anthropic 的 ``tool_result`` 块。"""
    return [
        {
            "type": "tool_result",
            "tool_use_id": message.get("tool_call_id") or "",
            "content": message.get("content") or "",
        }
        for message in messages
    ]


def to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 OpenAI 风格消息列表翻成 Anthropic 的 ``messages``。

    连续的 ``role=tool`` 合并成一条 user 消息里的多个 ``tool_result`` 块；
    连续同角色消息也做合并（Anthropic 要求交替），保证长会话不会因为合并失败被拒。
    """
    result: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        nonlocal pending_tool_results
        if pending_tool_results:
            result.append({"role": "user", "content": pending_tool_results})
            pending_tool_results = []

    for message in messages:
        role = message.get("role")
        if role == "tool":
            pending_tool_results.extend(_tool_result_blocks([message]))
            continue
        flush_tool_results()
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.get("content"):
                blocks.append({"type": "text", "text": message["content"]})
            for call in message.get("tool_calls") or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["function"]["name"],
                        "input": _parse_arguments(call["function"].get("arguments", "{}")),
                    }
                )
            if blocks:
                result.append({"role": "assistant", "content": blocks})
            continue
        text = message.get("content") or ""
        if role == "summary":
            text = f"[历史摘要] {text}"
        result.append({"role": "user", "content": [{"type": "text", "text": text}]})
    flush_tool_results()

    merged: list[dict[str, Any]] = []
    for message in result:
        if merged and merged[-1]["role"] == message["role"]:
            merged[-1]["content"].extend(message["content"])
        else:
            merged.append({"role": message["role"], "content": list(message["content"])})
    return merged


def to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for tool in tools:
        spec = tool.get("function", tool)
        converted.append(
            {
                "name": spec["name"],
                "description": spec.get("description", ""),
                "input_schema": spec.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


class ClaudeProvider:
    provider_name = "claude"

    def __init__(
        self,
        client: Any,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.client = client
        self.model = model
        self.max_tokens = max_tokens

    async def complete(self, request: CompletionRequest) -> ModelResponse:
        system_parts: list[str] = []
        leading: list[dict[str, Any]] = []
        for message in request.prefix:
            if message.get("role") == "system":
                system_parts.append(str(message.get("content") or ""))
            else:
                leading.append(dict(message))
        messages = to_anthropic_messages(leading + [dict(m) for m in request.messages])

        arguments: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        system = "\n\n".join(part for part in system_parts if part)
        if system:
            arguments["system"] = system
        tools = to_anthropic_tools([dict(tool) for tool in request.tools])
        if tools:
            arguments["tools"] = tools

        try:
            response = await self.client.messages.create(**arguments)
        except Exception as error:
            raise ProviderError(
                f"claude request failed: {type(error).__name__}",
                detail={"provider": self.provider_name, "model": self.model},
            ) from error

        blocks = getattr(response, "content", None) or []
        text = "".join(
            getattr(block, "text", "") for block in blocks if getattr(block, "type", "") == "text"
        )
        tool_calls = [
            ModelToolCall(
                id=getattr(block, "id", ""),
                name=getattr(block, "name", ""),
                arguments=json.dumps(getattr(block, "input", {}) or {}, ensure_ascii=False),
            )
            for block in blocks
            if getattr(block, "type", "") == "tool_use"
        ]
        usage = getattr(response, "usage", None)
        tokens = (getattr(usage, "input_tokens", 0) or 0) + (
            getattr(usage, "output_tokens", 0) or 0
        )
        finish_reason = "tool_calls" if tool_calls else getattr(response, "stop_reason", None)
        return ModelResponse(
            content=text or None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            tokens=tokens,
        )
