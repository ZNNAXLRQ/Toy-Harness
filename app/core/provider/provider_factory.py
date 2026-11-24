"""按配置返回 provider 实现。

输入是配置层已经解析好的 ``ResolvedProvider``（密钥、端点、模型都齐了），
输出是一个 ``LLMProvider``。这里**不读环境变量、不解析占位符**——那是
``app/core/config`` 的职责，工厂只做装配。

测试通过注入 ``client_factory`` 避免真实网络：``client_factory(adapter, resolved)``
返回 SDK 客户端（或任意具备相同形状的假对象）。
"""

from typing import Any, Callable

from app.common.api.llm_provider import LLMProvider
from app.common.exception import ConfigError
from app.core.config.settings import ResolvedProvider
from app.core.provider.claude_provider import DEFAULT_MODEL as CLAUDE_MODEL
from app.core.provider.claude_provider import ClaudeProvider
from app.core.provider.deepseek_provider import DEFAULT_BASE_URL as DEEPSEEK_BASE_URL
from app.core.provider.deepseek_provider import DEFAULT_MODEL as DEEPSEEK_MODEL
from app.core.provider.deepseek_provider import DeepSeekProvider
from app.core.provider.openai_provider import DEFAULT_MODEL as OPENAI_MODEL
from app.core.provider.openai_provider import OpenAIChatProvider

DEFAULT_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": DEEPSEEK_BASE_URL,
}

DEFAULT_MODELS: dict[str, str] = {
    "openai": OPENAI_MODEL,
    "deepseek": DEEPSEEK_MODEL,
    "claude": CLAUDE_MODEL,
}

ClientFactory = Callable[[str, ResolvedProvider], Any]


def _default_client_factory(adapter: str, resolved: ResolvedProvider) -> Any:
    """按 adapter 构造真实 SDK 客户端（延迟 import，避免测试时必须装/初始化 SDK）。"""
    if adapter in {"openai", "deepseek"}:
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=resolved.api_key,
            base_url=resolved.base_url or DEFAULT_BASE_URLS[adapter],
            timeout=resolved.timeout_seconds,
            max_retries=resolved.max_retries,
        )
    if adapter == "claude":
        from anthropic import AsyncAnthropic

        options: dict[str, Any] = {
            "api_key": resolved.api_key,
            "timeout": resolved.timeout_seconds,
            "max_retries": resolved.max_retries,
        }
        if resolved.base_url:
            options["base_url"] = resolved.base_url
        return AsyncAnthropic(**options)
    raise ConfigError(f"unknown adapter: {adapter!r}", detail={"adapter": adapter})


def build_provider(
    resolved: ResolvedProvider, *, client_factory: ClientFactory | None = None
) -> LLMProvider:
    """把一份解析好的 provider 配置装成 ``LLMProvider``。"""
    factory = client_factory or _default_client_factory
    adapter = resolved.adapter
    model = resolved.model or DEFAULT_MODELS.get(adapter)
    if model is None:
        raise ConfigError(
            f"provider {resolved.name!r} 的 adapter={adapter} 没有默认模型，请配置 llm.model",
            detail={"provider": resolved.name, "adapter": adapter},
        )
    client = factory(adapter, resolved)
    if adapter == "openai":
        return OpenAIChatProvider(client, model=model)
    if adapter == "deepseek":
        return DeepSeekProvider(client, model=model)
    if adapter == "claude":
        return ClaudeProvider(client, model=model)
    raise ConfigError(f"unknown adapter: {adapter!r}", detail={"adapter": adapter})


def create_provider(
    settings, *, client_factory: ClientFactory | None = None
) -> LLMProvider:
    """从 ``HarnessSettings`` 构造 provider（延迟解析：只有真要调模型时才调用）。"""
    return build_provider(settings.resolve_provider(), client_factory=client_factory)
