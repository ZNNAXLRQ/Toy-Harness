"""DeepSeek 适配器：与 OpenAI 兼容接口同形状，只差默认值与厂商名。

DeepSeek 的 ``base_url`` 默认 ``https://api.deepseek.com``（SDK 会自动补 ``/v1``），
密钥用 ``DEEPSEEK_API_KEY``，由配置层解析后以 ``ResolvedProvider`` 传入。
"""

from typing import Any

from app.core.provider.openai_provider import OpenAIChatProvider

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"


class DeepSeekProvider(OpenAIChatProvider):
    provider_name = "deepseek"

    def __init__(self, client: Any, *, model: str = DEFAULT_MODEL) -> None:
        super().__init__(client, model=model)
