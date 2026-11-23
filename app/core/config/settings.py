"""配置读取：``config/config.yaml`` 为真源，环境变量覆盖文件。

优先级（高 → 低）：显式传参 > ``HARNESS_*`` 环境变量 > ``.env`` > ``config.yaml`` > 代码默认值。

两条刻意的设计：

1. **空文件可用**。C/D 尚未填 ``config.yaml`` 时也必须能起服务，所以每个字段都有
   默认值，缺文件、空文件、缺字段都不会炸；真缺到不可用的程度由使用方抛
   ``ConfigError``（例如没有 API key）。
2. **不发明环境变量名**。密钥沿用厂商官方的 ``OPENAI_API_KEY`` /
   ``DEEPSEEK_API_KEY`` / ``ANTHROPIC_API_KEY``，与 ``llm.provider`` 一一对应；
   yaml 里写成 ``${DEEPSEEK_API_KEY}`` 占位符时，在这里解析成真实值。
   占位符与 provider 不一致属于配置错误（``ConfigError``），不做「尽力猜测」——
   否则切换厂商后会把上一家的密钥发给新的服务。
"""

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from app.common.exception import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"
ENV_FILE = PROJECT_ROOT / ".env"

ProviderName = Literal["deepseek", "openai", "claude"]

PROVIDER_ENV_KEYS: dict[str, str] = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
}


class ServerSettings(BaseModel):
    port: int = 8080


class DatabaseSettings(BaseModel):
    path: str = "./data/harness.db"


class LLMSettings(BaseModel):
    provider: ProviderName = "deepseek"
    api_key: str | None = None
    """可以写字面值、``${VAR}`` 占位符，或留空以便从厂商环境变量读取。"""

    base_url: str | None = None
    """留空时由具体适配器提供默认值（DeepSeek/OpenAI 走兼容接口，Claude 无自定义）。"""

    model: str | None = None
    """留空时由适配器选择默认模型，配置文件不该硬编码模型版本。"""

    timeout_seconds: float = 60.0
    max_retries: int = 0


class LoggingSettings(BaseModel):
    level: str = "INFO"


class HarnessSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HARNESS_",
        env_nested_delimiter="__",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    server: ServerSettings = Field(default_factory=ServerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """yaml 插在 dotenv 之后：文件提供基线，环境变量负责覆盖。"""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls, yaml_file=CONFIG_FILE),
            file_secret_settings,
        )

    @property
    def resolved_api_key(self) -> str | None:
        """按 provider 解析出真实密钥；没有则 ``None``（由容器决定是否报错）。

        占位符必须指向**当前 provider** 对应的变量：``provider`` 被
        ``HARNESS_LLM__PROVIDER`` 之类的覆盖切到另一家、而 yaml 里还留着上一家的
        ``${DEEPSEEK_API_KEY}`` 时，直接把那个密钥交给新适配器就是把凭据发给错误的
        服务。这种情况视为配置错误，抛 ``ConfigError`` 而不是「尽力解析」。
        """
        raw = (self.llm.api_key or "").strip()
        expected = PROVIDER_ENV_KEYS[self.llm.provider]
        if raw.startswith("${") and raw.endswith("}"):
            name = raw[2:-1].strip()
            if name != expected:
                raise ConfigError(
                    f"llm.api_key 指向 {name}，但 llm.provider 是 {self.llm.provider}；"
                    f"切换厂商时必须一起改占位符（应为 {expected}）",
                    detail={"provider": self.llm.provider, "placeholder": name, "expected": expected},
                )
            return os.environ.get(name) or None
        if raw:
            return raw
        return os.environ.get(expected) or None


def load_env(path: Path = ENV_FILE) -> None:
    """把 ``.env`` 里的厂商密钥载入进程环境；文件不存在时静默跳过。

    由容器在启动时调用一次。不放在 import 时执行——导入模块不应有副作用。
    """
    if path.is_file():
        load_dotenv(path, override=False)


def load_settings() -> HarnessSettings:
    """读取配置；``config/config.yaml`` 缺失或为空都能返回一份可用的默认配置。"""
    load_env()
    return HarnessSettings()
