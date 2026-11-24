"""模型调用端口。

上层（core/loop）只依赖这里的形状；各家 SDK 的差异在 ``app/core/provider/``
内部吸收，不允许把 ``openai``/``anthropic`` 的类型泄漏到端口之外。

**前缀稳定性契约（prefix cache 的前提）**：

- ``CompletionRequest.prefix`` 是一个会话的稳定前缀：system 提示、AGENTS.md
  内容、工具定义。同一会话内逐轮必须**逐字节一致**。
- 因此 prefix 里不许出现时间戳、计数器、随机 id、随轮次变化的工具列表。
- 预算裁剪只允许动 ``messages``（尾部），永远不动 ``prefix``；否则每轮都会
  击穿供应商的 prefix cache。

**不做 token 级流式**：SSE 推送的是 agent 事件而不是 token delta；需要流式时
再追加 ``astream``，不改变本端口的现有语义。
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class ModelToolCall:
    """模型请求的一次工具调用（瞬态对象，不落库）。

    落库后的对应实体是 ``ToolCallRecord``（C 的 ``common/model/tool_call.py``）。
    ``arguments`` 保留模型给出的**原始 JSON 文本**，由执行层解析并校验；
    端口不做提前解析，避免把「模型给了坏 JSON」变成 provider 层的错误。
    """

    id: str
    name: str
    arguments: str


@dataclass
class ModelResponse:
    """一次模型调用的结果。"""

    content: str | None = None
    tool_calls: list[ModelToolCall] = field(default_factory=list)
    finish_reason: str | None = "stop"
    tokens: int = 0
    reasoning_content: str | None = None

    def to_message(self) -> dict[str, Any]:
        """转成 OpenAI 兼容的历史消息形状。

        **仅用于进程内拼装请求**。落库请走 ``StateStore.record_assistant_turn``：
        ``reasoning_content`` 不进数据库（schema 无列），恢复后的历史只有可见
        content。若把本方法的结果当作「可恢复的历史真相」，重启后两者就会不一致。
        """
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        if self.reasoning_content is not None:
            message["reasoning_content"] = self.reasoning_content
        return message


@dataclass(frozen=True)
class CompletionRequest:
    """一次补全请求。

    ``prefix`` 与 ``messages`` 分开传，既对上 Anthropic 的 ``system`` 参数，
    也让「前缀不可变」成为接口层面的显式契约而不是口头约定。
    """

    prefix: Sequence[Mapping[str, Any]]
    messages: Sequence[Mapping[str, Any]]
    tools: Sequence[Mapping[str, Any]] = ()


class LLMProvider(Protocol):
    """模型供应商端口。实现：OpenAI / DeepSeek（兼容接口）/ Claude。"""

    provider_name: str

    async def complete(self, request: CompletionRequest) -> ModelResponse:
        """执行一次非流式补全；失败必须抛 ``ProviderError`` 而不是 SDK 异常。"""
        ...
