"""子代理的独立上下文。

子代理**不写主会话**：它的消息列表只存在内存里（schema 没有 parent 列，子代理的完整
transcript 不落库；落库的是 ``subagent_runs`` 的 brief 与结果，见
``IMPLEMENTATION_NOTES`` 的已知限制）。因此这里是一个纯粹的对话容器，不做持久化、
不做摘要（预算内有界）。

它在进程内的生命周期 = 一次 ``spawn``；父会话的 ``messages`` / ``tool_calls`` 不会被它
改写，这是「独立上下文」的硬要求。
"""

import json
from dataclasses import dataclass, field
from typing import Any

from app.common.api.llm_provider import ModelResponse
from app.common.api.subagent_scheduler import SubagentTask
from app.common.api.tool import ToolResult


@dataclass
class SubagentContext:
    task: SubagentTask
    max_iterations: int
    token_budget: int
    messages: list[dict[str, Any]] = field(default_factory=list)
    tokens: int = 0
    steps: int = 0

    def __post_init__(self) -> None:
        self.messages.append({"role": "user", "content": self.task.prompt})

    def to_wire(self) -> list[dict[str, Any]]:
        return list(self.messages)

    def add_model_response(self, response: ModelResponse) -> None:
        """记录一次模型响应；``reasoning_content`` 不放进 wire（跨供应商不安全）。"""
        self.steps += 1
        self.tokens += response.tokens
        message: dict[str, Any] = {"role": "assistant", "content": response.content}
        if response.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in response.tool_calls
            ]
        self.messages.append(message)

    def add_tool_result(self, call_id: str, result: ToolResult) -> None:
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(result.to_dict(), ensure_ascii=False),
            }
        )

    @property
    def last_content(self) -> str | None:
        for message in reversed(self.messages):
            if message.get("role") == "assistant" and message.get("content"):
                return message["content"]
        return None

    def budget_exceeded(self) -> bool:
        return self.tokens > self.token_budget

    def iteration_exhausted(self) -> bool:
        return self.steps >= self.max_iterations
