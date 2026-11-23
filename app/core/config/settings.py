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
from pydantic import BaseModel, ConfigDict, Field, field_validator
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
    model_config = ConfigDict(extra="forbid")

    port: int = Field(default=8080, ge=1, le=65535)


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = "./data/harness.db"

    @field_validator("path")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("database.path must not be empty")
        return value


class WorkspaceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = "./workspace"
    """工具的工作区根目录；相对路径按项目根解析（与 database.path 一致）。"""

    @field_validator("path")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("workspace.path must not be empty")
        return value


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_iterations: int = Field(default=15, ge=1)
    token_budget: int = Field(default=32000, ge=1024)


class SubagentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrency: int = Field(default=2, ge=1)
    max_depth: int = Field(default=2, ge=1)


class ProviderProfile(BaseModel):
    """一条完整的 provider 配置。预设与自定义是同一个形状，只差填不填 optional 字段。

    ``extra="forbid"``：字段名拼错（``api_keyy``）必须立刻报错，而不是被静默忽略、
    然后在启动时抛出一句莫名其妙的「没有可用密钥」。
    """

    model_config = ConfigDict(extra="forbid")

    adapter: AdapterName = "deepseek"
    """协议形状，决定用哪个适配器、以及缺省域名取哪家的。"""

    api_key: str | None = Field(default=None, repr=False)
    """字面值、``${VAR}`` 占位符，或留空以使用适配器对应的厂商环境变量。

    ``repr=False``：日志里 ``print(settings)`` 不该把密钥打出来。注意
    ``model_dump()`` / ``model_dump_json()`` **仍会包含它**，不要把整个配置对象
    直接写日志或返回给前端。
    """

    base_url: str | None = None
    """留空表示用适配器的默认域名；填写则覆盖（自建网关、OpenRouter、vLLM 等）。"""

    model: str | None = None
    """留空由适配器选择默认模型，配置文件不硬编码模型版本。"""

    timeout_seconds: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=0, ge=0)

    @field_validator("api_key", "base_url", "model", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """``api_key: ""`` 这类空字符串按「没填」处理，避免把空串当成字面值发出去。"""
        if isinstance(value, str) and not value.strip():
            return None
        return value


def _default_profiles() -> dict[str, ProviderProfile]:
    """三家预设；``config.yaml`` 未写 ``providers`` 时生效。"""
    return {name: ProviderProfile(adapter=name) for name in ADAPTER_ENV_KEYS}


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: str = "deepseek"
    providers: dict[str, ProviderProfile] = Field(default_factory=_default_profiles)


class LoggingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str = "INFO"

    @field_validator("level")
    @classmethod
    def _known_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"unknown logging level: {value!r}")
        return normalized


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
        # 拼错的顶层键必须报错：``llm.activ: openai`` 静默回落 deepseek 是这里最贵的
        # 一次误配置（用户以为换了厂商，实际还在用上一家）。新增 section 要走显式改代码。
        extra="forbid",
    )

    server: ServerSettings = Field(default_factory=ServerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    subagent: SubagentSettings = Field(default_factory=SubagentSettings)
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

        **建议容器延迟调用它**：没有配置密钥时服务仍应能启动并浏览历史会话，
        只有第一次真正要调模型时才需要 provider。启动即解析会让「只想看看 UI」
        也被密钥卡住。
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

    def database_path(self) -> Path:
        """SQLite 文件路径。

        相对路径按**项目根目录**解析，而不是进程 cwd——否则换个目录启动服务就会
        另起一个空的 ``data/harness.db``，看起来像「数据丢了」。
        """
        return _resolve_under_project_root(self.database.path)

    def workspace_path(self) -> Path:
        """工具工作区根目录；相对路径同样按项目根解析。"""
        return _resolve_under_project_root(self.workspace.path)


def _resolve_under_project_root(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _resolve_api_key(profile: ProviderProfile, name: str) -> str | None:
    """解析成真实密钥。

    规则（按顺序）：

    1. ``${VAR}`` 占位符：变量名必须非空；如果它指向**另一家厂商的官方变量**（含
       小写写法）视为抄错行，直接报错；
    2. 字面值：原样返回；
    3. 留空：只有**使用适配器默认域名**时才回落到厂商环境变量。
       **自定义 ``base_url`` 必须显式写 api_key**——否则会把厂商官方密钥发给一个
       任意地址（自建网关、代理、第三方中转），这是比"没配 key"严重得多的错误；
    4. 以 ``$`` 开头但不是合法 ``${NAME}`` 的写法（``$DEEPSEEK_API_KEY``、
       ``${DEEPSEEK_API_KEY``）视为坏配置，而不是当成字面密钥去发 401。
    """
    raw = (profile.api_key or "").strip()
    expected = ADAPTER_ENV_KEYS[profile.adapter]

    if raw.startswith("${"):
        if not raw.endswith("}"):
            raise ConfigError(
                f"profile {name!r} 的 api_key 占位符缺少右花括号：{raw!r}；"
                f"应写成 ${{{expected}}} 这样的形式",
                detail={"provider": name, "adapter": profile.adapter},
            )
        variable = raw[2:-1].strip()
        if not variable:
            raise ConfigError(
                f"profile {name!r} 的 api_key 是空占位符 ``${{}}``；"
                f"应写变量名（如 ${{{expected}}}）或留空",
                detail={"provider": name, "adapter": profile.adapter},
            )
        canonical = variable.upper()
        if canonical in KNOWN_VENDOR_VARS and canonical != expected:
            raise ConfigError(
                f"profile {name!r}（adapter={profile.adapter}）的 api_key 指向 {variable}，"
                f"这属于另一家厂商；应使用 {expected}（或自定义变量名）",
                detail={"provider": name, "adapter": profile.adapter, "placeholder": variable},
            )
        return os.environ.get(variable) or None

    if raw.startswith("$"):
        raise ConfigError(
            f"profile {name!r} 的 api_key 看起来是坏的占位符：{raw!r}；"
            f"占位符必须写成 ${{变量名}}，字面密钥不要以 $ 开头",
            detail={"provider": name, "adapter": profile.adapter},
        )

    if raw:
        return raw

    if profile.base_url:
        raise ConfigError(
            f"profile {name!r} 使用了自定义 base_url（{profile.base_url}），"
            f"但没有显式填写 api_key；请写明 ${{{expected}}} 或其他变量名，"
            f"避免把厂商官方密钥发给非官方地址",
            detail={"provider": name, "adapter": profile.adapter, "base_url": profile.base_url},
        )
    return os.environ.get(expected) or None


def load_env(path: Path = ENV_FILE) -> None:
    """把 ``.env`` 里的厂商密钥载入进程环境；文件不存在时静默跳过。

    由容器在启动时调用一次。不放在 import 时执行——导入模块不应有副作用。
    """
    if path.is_file():
        load_dotenv(path, override=False)


KNOWN_SECTIONS = frozenset(
    {"server", "database", "workspace", "llm", "agent", "subagent", "logging"}
)
"""配置里允许出现的顶层 section。新增 section 必须同时改这里（见 extra="forbid"）。"""


def check_env_sections() -> None:
    """检查 ``HARNESS_*`` 环境变量的 section 名是否拼对。

    pydantic-settings 对拼错的环境变量段名是**静默忽略**的（``HARNESS_LLMM__ACTIVE``
    不会报错，只会让人以为切了厂商其实没有）。YAML 侧靠 ``extra="forbid"`` 挡住了，
    环境变量侧只能自己扫一遍。
    """
    unknown = {}
    for key in os.environ:
        if not key.startswith("HARNESS_"):
            continue
        section = key[len("HARNESS_") :].split("__", 1)[0].lower()
        if section and section not in KNOWN_SECTIONS:
            unknown[key] = section
    if unknown:
        listing = ", ".join(f"{key}（section={section!r}）" for key, section in sorted(unknown.items()))
        raise ConfigError(
            f"未知的配置 section：{listing}；允许的 section 是 {sorted(KNOWN_SECTIONS)}",
            detail={"unknown": sorted(unknown)},
        )


def load_settings() -> HarnessSettings:
    """读取配置；``config/config.yaml`` 缺失或为空都能返回一份可用的默认配置。

    额外做一次环境变量 section 名校验（拼错的 ``HARNESS_*`` 会被 pydantic-settings
    静默忽略，这里补上）。
    """
    load_env()
    check_env_sections()
    return HarnessSettings()
