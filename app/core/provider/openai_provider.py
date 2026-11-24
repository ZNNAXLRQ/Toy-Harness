"""OpenAI 兼容适配器（OpenAI 与 DeepSeek 共用同一形状）。

职责只有三件：把 ``CompletionRequest`` 拼成 SDK 参数、把响应翻成 ``ModelResponse``、
把 SDK 异常翻成 ``ProviderError``。**不做重试、不做流式、不认识配置**——重试由
``ResolvedProvider.max_retries`` 传给 SDK，配置由工厂负责。

前缀契约：``prefix`` 里的消息**原样**放在最前面（目前是 system），不重新排序、
不插入时间戳；这样供应商的 prefix cache 才命中。
"""

from typing import Any

from app.common.api.llm_provider import (
    CompletionRequest,
    ModelResponse,
    ModelToolCall,
)
from app.common.exception import ProviderError

DEFAULT_MODEL = "gpt-4o-mini"
"""兜底模型；``config.yaml`` 的 ``llm.providers.<name>.model`` 可覆盖。"""


class OpenAIChatProvider:
    """Chat Completions 形状的适配器（DeepSeek 复用）。"""

    provider_name = "openai"

    def __init__(self, client: Any, *, model: str = DEFAULT_MODEL) -> None:
        self.client = client
        self.model = model

    async def complete(self, request: CompletionRequest) -> ModelResponse:
        messages: list[dict[str, Any]] = [dict(message) for message in request.prefix]
        messages.extend(dict(message) for message in request.messages)
        arguments: dict[str, Any] = {"model": self.model, "messages": messages}
        tools = [dict(tool) for tool in request.tools]
        if tools:
            arguments.update(tools=tools, tool_choice="auto")

        try:
            response = await self.client.chat.completions.create(**arguments)
        except Exception as error:  # SDK 异常在此翻译，不泄漏到 core 之外
            raise ProviderError(
                f"{self.provider_name} request failed: {type(error).__name__}",
                detail={"provider": self.provider_name, "model": self.model},
            ) from error

        choices = getattr(response, "choices", None)
        if not choices:
            raise ProviderError(
                f"{self.provider_name} returned no choices",
                detail={"provider": self.provider_name, "model": self.model},
            )
        choice = choices[0]
        message = choice.message
        usage = getattr(response, "usage", None)
        return ModelResponse(
            content=getattr(message, "content", None),
            tool_calls=[
                ModelToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=call.function.arguments,
                )
                for call in (getattr(message, "tool_calls", None) or [])
            ],
            finish_reason=getattr(choice, "finish_reason", None),
            tokens=getattr(usage, "total_tokens", 0) or 0,
            reasoning_content=getattr(message, "reasoning_content", None),
        )
