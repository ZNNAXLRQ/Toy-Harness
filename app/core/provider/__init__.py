"""模型适配层：把各家 SDK 的差异吸收在这里，core 只依赖 ``common/api`` 端口。

``provider_factory.build_provider`` 是唯一入口；配置层（``core/config``）负责解析
密钥/端点/模型，本层不读环境变量。
"""

from app.core.provider.claude_provider import ClaudeProvider
from app.core.provider.deepseek_provider import DeepSeekProvider
from app.core.provider.openai_provider import OpenAIChatProvider
from app.core.provider.provider_factory import build_provider, create_provider

__all__ = [
    "ClaudeProvider",
    "DeepSeekProvider",
    "OpenAIChatProvider",
    "build_provider",
    "create_provider",
]
