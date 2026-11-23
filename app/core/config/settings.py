"""配置读取：``config/config.yaml`` 为真源，环境变量覆盖文件。

优先级（高 → 低）：显式传参 > ``HARNESS_*`` 环境变量 > ``.env`` > ``config.yaml`` > 代码默认值。

**配置的基本单位是 provider profile，不是单个字段。** 切换厂商只改 ``llm.active``
一个字段；密钥、端点、模型都绑在同一条 profile 里，因此不存在「provider 换了、
base_url / api_key 还留在上一家」的中间态：

    llm:
      active: deepseek
      providers:
        deepseek: { adapter: deepseek, api_key: ${DEEPSEEK_API_KEY} }
        gateway:  { adapter: openai, base_url: https://my-gw/v1, api_key: ${GW_KEY} }

分工：厂商默认域名、环境变量名这类事实属于**代码**（各适配器）；配置文件只声明
「用哪套、钥匙在哪、要不要覆盖端点」。``providers`` 在 yaml 里出现即整段替换代码
默认值（pydantic 不做深合并），避免"我看着改了一条、其实还在用默认值"。
"""

import os
from dataclasses import dataclass, field
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

AdapterName = Literal["deepseek", "openai", "claude"]

# 适配器名 → 厂商官方环境变量名。事实属于代码，不写进配置文件。
ADAPTER_ENV_KEYS: dict[str, str] = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
}
KNOWN_VENDOR_VARS = frozenset(ADAPTER_ENV_KEYS.values())


class ServerSettings(BaseModel):
    port: int = 8080


class DatabaseSettings(BaseModel):
    path: str = "./data/harness.db"


class ProviderProfile(BaseModel):
    """一条完整的 provider 配置。预设与自定义是同一个形状，只差填不填 optional 字段。"""

    adapter: AdapterName = "deepseek"
    """协议形状，决定用哪个适配器、以及缺省域名取哪家的。"""

    api_key: str | None = Field(default=None, repr=False)
    """字面值、``${VAR}`` 占位符，或留空以使用适配器对应的厂商环境变量。

    ``repr=False``：日志里 ``print(settings)`` 不该把密钥打出来。
    """

    base_url: str | None = None
    """留空表示用适配器的默认域名；填写则覆盖（自建网关、OpenRouter、vLLM 等）。"""

    model: str | None = None
    """留空由适配器选择默认模型，配置文件不硬编码模型版本。"""

    timeout_seconds: float = 60.0
    max_retries: int = 0


def _default_profiles() -> dict[str, ProviderProfile]:
    """三家预设；``config.yaml`` 未写 ``providers`` 时生效。"""
    return {name: ProviderProfile(adapter=name) for name in ADAPTER_ENV_KEYS}


class LLMSettings(BaseModel):
    active: str = "deepseek"
    providers: dict[str, ProviderProfile] = Field(default_factory=_default_profiles)


class LoggingSettings(BaseModel):
    level: str = "INFO"


@dataclass(frozen=True)
class ResolvedProvider:
    """补齐默认值之后、可以直接交给工厂的东西。

    ``base_url`` 为 ``None`` 表示"用适配器默认域名"，由适配器决定而不是配置层猜。
    """

    name: str
    adapter: str
    api_key: str = field(repr=False)
    """``repr=False``：这个对象会流经日志与调试输出，密钥不该出现在里面。"""
    base_url: str | None
    model: str | None
    timeout_seconds: float
    max_retries: int


class HarnessSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HARNESS_",
        env_nested_delimiter="__",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        # 必须显式指定：默认按本地编码读 yaml，中文 Windows 上是 GBK，
        # 配置文件里只要有一行中文注释就会 UnicodeDecodeError。
        yaml_file_encoding="utf-8",
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

    def resolve_provider(self) -> ResolvedProvider:
        """把 ``llm.active`` 指向的 profile 解析成可直接使用的配置。

        失败一律抛 ``ConfigError``（不是 pydantic 的 ValidationError），因为这是
        部署配置问题，Web 层要能映射成一个可读的错误而不是 500 堆栈。
        """
        name = self.llm.active
        profile = self.llm.providers.get(name)
        if profile is None:
            raise ConfigError(
                f"llm.active = {name!r} 没有对应的 profile；"
                f"可选：{sorted(self.llm.providers)}",
                detail={"active": name, "available": sorted(self.llm.providers)},
            )

        api_key = _resolve_api_key(profile, name)
        if not api_key:
            raise ConfigError(
                f"provider {name!r}（adapter={profile.adapter}）没有可用密钥；"
                f"请在 profile 里填 api_key，或设置环境变量 {ADAPTER_ENV_KEYS[profile.adapter]}",
                detail={"provider": name, "adapter": profile.adapter},
            )

        return ResolvedProvider(
            name=name,
            adapter=profile.adapter,
            api_key=api_key,
            base_url=profile.base_url,
            model=profile.model,
            timeout_seconds=profile.timeout_seconds,
            max_retries=profile.max_retries,
        )


def _resolve_api_key(profile: ProviderProfile, name: str) -> str | None:
    """解析成真实密钥。

    占位符允许指向任意变量（自定义网关用 ``${GW_KEY}`` 是合法的），但如果它指的
    是**另一家厂商的官方变量**（``${OPENAI_API_KEY}`` 却挂在 deepseek 上），那基本
    是从示例配置里抄错了行，直接报错；配置串味在 profile 模型里本就不可能发生。
    """
    raw = (profile.api_key or "").strip()
    expected = ADAPTER_ENV_KEYS[profile.adapter]
    if raw.startswith("${") and raw.endswith("}"):
        variable = raw[2:-1].strip()
        if variable in KNOWN_VENDOR_VARS and variable != expected:
            raise ConfigError(
                f"profile {name!r}（adapter={profile.adapter}）的 api_key 指向 {variable}，"
                f"这属于另一家厂商；应使用 {expected}（或自定义变量名）",
                detail={"provider": name, "adapter": profile.adapter, "placeholder": variable},
            )
        return os.environ.get(variable) or None
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
