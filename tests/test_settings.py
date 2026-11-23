"""配置读取的契约：空文件可用、yaml 是基线、环境变量覆盖、provider 是成套切换的。"""

import pytest
from pydantic import ValidationError

from app.common.exception import ConfigError
from app.core.config import settings as settings_module
from app.core.config.settings import PROJECT_ROOT, HarnessSettings

SAMPLE_YAML = """
server:
  port: 9000

database:
  path: ./data/custom.db

llm:
  active: deepseek
  providers:
    deepseek:
      adapter: deepseek
      api_key: ${DEEPSEEK_API_KEY}
      base_url: https://api.deepseek.com
    gateway:
      adapter: openai
      api_key: ${GW_KEY}
      base_url: https://my-gw/v1
      model: qwen-max
      timeout_seconds: 30
      max_retries: 2

logging:
  level: DEBUG
"""


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(settings_module, "CONFIG_FILE", path)
    return path


@pytest.fixture
def sample_config(config_file):
    config_file.write_text(SAMPLE_YAML, encoding="utf-8")
    return config_file


def test_missing_config_file_uses_defaults(config_file):
    settings = HarnessSettings()

    assert settings.server.port == 8080
    assert settings.database.path == "./data/harness.db"
    assert settings.llm.active == "deepseek"
    assert set(settings.llm.providers) == {"deepseek", "openai", "claude"}
    assert settings.logging.level == "INFO"


def test_empty_config_file_uses_defaults(config_file):
    """C/D 尚未填 config.yaml 时服务也必须能起。"""
    config_file.write_text("", encoding="utf-8")

    assert HarnessSettings().llm.active == "deepseek"


def test_yaml_provides_the_baseline(sample_config):
    settings = HarnessSettings()

    assert settings.server.port == 9000
    assert settings.database.path == "./data/custom.db"
    assert settings.logging.level == "DEBUG"
    assert set(settings.llm.providers) == {"deepseek", "gateway"}  # yaml 写 providers 即整段替换


def test_switching_active_switches_the_whole_profile(sample_config, monkeypatch):
    """换 provider 只动一个字段：密钥、端点、模型一起换，不存在换了一半。"""
    monkeypatch.setenv("HARNESS_LLM__ACTIVE", "gateway")
    monkeypatch.setenv("GW_KEY", "sk-gateway")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")

    resolved = HarnessSettings().resolve_provider()

    assert resolved.name == "gateway"
    assert resolved.adapter == "openai"
    assert resolved.api_key == "sk-gateway"
    assert resolved.base_url == "https://my-gw/v1"
    assert resolved.model == "qwen-max"
    assert resolved.timeout_seconds == 30
    assert resolved.max_retries == 2


def test_preset_profile_keeps_its_own_key(sample_config, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    monkeypatch.setenv("GW_KEY", "sk-gateway")

    resolved = HarnessSettings().resolve_provider()

    assert resolved.name == "deepseek"
    assert resolved.adapter == "deepseek"
    assert resolved.api_key == "sk-deepseek"
    assert resolved.base_url == "https://api.deepseek.com"


def test_placeholder_naming_another_vendor_is_rejected(config_file, monkeypatch):
    """把 openai 的变量名抄到 deepseek profile 上：报错，不静默取用。"""
    config_file.write_text(
        "llm:\n  active: deepseek\n  providers:\n"
        "    deepseek:\n      adapter: deepseek\n      api_key: ${OPENAI_API_KEY}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")

    with pytest.raises(ConfigError):
        HarnessSettings().resolve_provider()


def test_custom_profile_may_use_its_own_env_var(sample_config, monkeypatch):
    """自定义网关的密钥变量名是自由的，不该被厂商变量名限制。"""
    monkeypatch.setenv("HARNESS_LLM__ACTIVE", "gateway")
    monkeypatch.setenv("GW_KEY", "sk-custom")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert HarnessSettings().resolve_provider().api_key == "sk-custom"


def test_literal_api_key_is_used_as_is(config_file, monkeypatch):
    config_file.write_text(
        "llm:\n  active: local\n  providers:\n"
        "    local:\n      adapter: openai\n      api_key: literal-key\n"
        "      base_url: http://localhost:8000/v1\n",
        encoding="utf-8",
    )

    resolved = HarnessSettings().resolve_provider()

    assert resolved.api_key == "literal-key"
    assert resolved.base_url == "http://localhost:8000/v1"


def test_utf8_config_with_chinese_comments_is_readable(config_file):
    """中文 Windows 默认按 GBK 读 yaml；显式 utf-8 之前，一行中文注释就能让启动失败。"""
    config_file.write_text(
        "# 服务端口\n"
        "server:\n"
        "  port: 9100  # 端口\n"
        "llm:\n"
        "  active: deepseek  # 当前生效的厂商\n"
        "  providers:\n"
        "    deepseek:\n"
        "      api_key: literal-key  # 本地测试用\n",
        encoding="utf-8",
    )

    settings = HarnessSettings()

    assert settings.server.port == 9100
    assert settings.resolve_provider().api_key == "literal-key"


def test_repr_does_not_leak_literal_api_key(config_file):
    """日志里 print(settings) / print(resolved) 不能把密钥打出来。"""
    config_file.write_text(
        "llm:\n  active: local\n  providers:\n"
        "    local:\n      adapter: openai\n      api_key: literal-secret-value\n",
        encoding="utf-8",
    )

    settings = HarnessSettings()
    resolved = settings.resolve_provider()

    assert "literal-secret-value" not in repr(settings)
    assert "literal-secret-value" not in repr(resolved)
    assert resolved.api_key == "literal-secret-value"


def test_missing_api_key_raises_with_actionable_message(config_file, monkeypatch):
    config_file.write_text(
        "llm:\n  active: deepseek\n  providers:\n    deepseek:\n      adapter: deepseek\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(ConfigError) as caught:
        HarnessSettings().resolve_provider()

    assert "DEEPSEEK_API_KEY" in caught.value.message


def test_unknown_active_profile_raises(config_file):
    config_file.write_text("llm:\n  active: nope\n", encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        HarnessSettings().resolve_provider()

    assert "nope" in caught.value.message
    assert "deepseek" in caught.value.message  # 给出可选值


def test_profile_defaults_apply_when_not_overridden(config_file):
    config_file.write_text(
        "llm:\n  active: deepseek\n  providers:\n    deepseek:\n      api_key: k\n",
        encoding="utf-8",
    )

    resolved = HarnessSettings().resolve_provider()

    assert resolved.adapter == "deepseek"
    assert resolved.base_url is None  # 留给适配器的默认域名
    assert resolved.timeout_seconds == 60.0
    assert resolved.max_retries == 0


# --- 全链路自查补的用例 ---


def test_typo_in_profile_field_is_rejected(config_file):
    """字段拼错必须报错，而不是被静默忽略成「没有密钥」。"""
    config_file.write_text(
        "llm:\n  active: local\n  providers:\n    local:\n      adapter: openai\n      api_keyy: sk\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        HarnessSettings()


def test_invalid_server_port_is_rejected(config_file):
    config_file.write_text("server:\n  port: 99999\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        HarnessSettings()


def test_non_positive_timeout_is_rejected(config_file):
    config_file.write_text(
        "llm:\n  active: local\n  providers:\n    local:\n      api_key: k\n      timeout_seconds: 0\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        HarnessSettings()


def test_blank_api_key_is_treated_as_missing(config_file, monkeypatch):
    """``api_key: ""`` 按没填处理，回落到厂商环境变量，不把空串当字面值发出去。"""
    config_file.write_text(
        'llm:\n  active: local\n  providers:\n    local:\n      adapter: openai\n      api_key: ""\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")

    assert HarnessSettings().resolve_provider().api_key == "sk-env"


def test_lowercase_placeholder_naming_another_vendor_is_rejected(config_file, monkeypatch):
    config_file.write_text(
        "llm:\n  active: local\n  providers:\n    local:\n      adapter: deepseek\n"
        "      api_key: ${openai_api_key}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("openai_api_key", "sk-openai")

    with pytest.raises(ConfigError):
        HarnessSettings().resolve_provider()


def test_empty_placeholder_is_rejected(config_file):
    config_file.write_text(
        "llm:\n  active: local\n  providers:\n    local:\n      api_key: ${}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError):
        HarnessSettings().resolve_provider()


def test_relative_database_path_resolves_against_project_root(config_file):
    """相对路径不能依赖进程 cwd，否则换个目录启动就像「数据丢了」。"""
    config_file.write_text("database:\n  path: ./data/harness.db\n", encoding="utf-8")

    settings = HarnessSettings()

    assert settings.database_path() == (PROJECT_ROOT / "data/harness.db").resolve()

# --- 配置：新增 section、拼错键、坏占位符、自定义端点 ---


def test_new_sections_have_defaults(config_file):
    config_file.write_text("", encoding="utf-8")
    settings = HarnessSettings()

    assert settings.workspace.path == "./workspace"
    assert settings.agent.max_iterations == 15
    assert settings.agent.token_budget == 32000
    assert settings.subagent.max_concurrency == 2
    assert settings.subagent.max_depth == 2


def test_unknown_top_level_key_is_rejected(config_file):
    """``llm.activ: openai`` 这种拼错必须报错，而不是静默继续用 deepseek。"""
    config_file.write_text("llm:\n  activ: openai\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        HarnessSettings()


def test_unknown_section_field_is_rejected(config_file):
    config_file.write_text("agent:\n  max_iterations: 5\n  turbo: true\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        HarnessSettings()


@pytest.mark.parametrize("broken", ["$DEEPSEEK_API_KEY", "${DEEPSEEK_API_KEY"])
def test_malformed_placeholder_is_rejected(config_file, broken):
    config_file.write_text(
        "llm:\n  active: local\n  providers:\n    local:\n      adapter: deepseek\n"
        f'      api_key: "{broken}"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError):
        HarnessSettings().resolve_provider()


def test_custom_base_url_requires_explicit_api_key(config_file, monkeypatch):
    """自定义端点不能静默回落到厂商官方密钥。"""
    config_file.write_text(
        "llm:\n  active: gateway\n  providers:\n    gateway:\n      adapter: openai\n"
        "      base_url: http://localhost:8000/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-official")

    with pytest.raises(ConfigError):
        HarnessSettings().resolve_provider()


def test_custom_base_url_with_explicit_placeholder_is_allowed(config_file, monkeypatch):
    config_file.write_text(
        "llm:\n  active: gateway\n  providers:\n    gateway:\n      adapter: openai\n"
        "      base_url: http://localhost:8000/v1\n      api_key: ${OPENAI_API_KEY}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-official")

    resolved = HarnessSettings().resolve_provider()

    assert resolved.base_url == "http://localhost:8000/v1"
    assert resolved.api_key == "sk-official"


def test_invalid_logging_level_is_rejected(config_file):
    config_file.write_text("logging:\n  level: LOUD\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        HarnessSettings()


def test_empty_database_path_is_rejected(config_file):
    config_file.write_text("database:\n  path: \"\"\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        HarnessSettings()


def test_workspace_path_resolves_against_project_root(config_file):
    config_file.write_text("workspace:\n  path: ./workspace\n", encoding="utf-8")

    assert HarnessSettings().workspace_path() == (PROJECT_ROOT / "workspace").resolve()
