"""配置读取的契约：空文件可用、yaml 是基线、环境变量覆盖、密钥按 provider 解析。"""

import pytest

from app.common.exception import ConfigError
from app.core.config import settings as settings_module
from app.core.config.settings import HarnessSettings

SAMPLE_YAML = """
server:
  port: 9000

database:
  path: ./data/custom.db

llm:
  provider: deepseek
  api_key: ${DEEPSEEK_API_KEY}
  base_url: https://api.deepseek.com

logging:
  level: DEBUG
"""


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(settings_module, "CONFIG_FILE", path)
    return path


def test_missing_config_file_uses_defaults(config_file):
    settings = HarnessSettings()

    assert settings.server.port == 8080
    assert settings.database.path == "./data/harness.db"
    assert settings.llm.provider == "deepseek"
    assert settings.logging.level == "INFO"


def test_empty_config_file_uses_defaults(config_file):
    """C/D 尚未填 config.yaml 时服务也必须能起。"""
    config_file.write_text("", encoding="utf-8")

    assert HarnessSettings().server.port == 8080


def test_yaml_provides_the_baseline(config_file):
    config_file.write_text(SAMPLE_YAML, encoding="utf-8")
    settings = HarnessSettings()

    assert settings.server.port == 9000
    assert settings.database.path == "./data/custom.db"
    assert settings.logging.level == "DEBUG"


def test_env_overrides_yaml(config_file, monkeypatch):
    config_file.write_text(SAMPLE_YAML, encoding="utf-8")
    monkeypatch.setenv("HARNESS_SERVER__PORT", "9999")
    monkeypatch.setenv("HARNESS_LLM__PROVIDER", "openai")

    settings = HarnessSettings()

    assert settings.server.port == 9999
    assert settings.llm.provider == "openai"


def test_placeholder_api_key_is_resolved_from_env(config_file, monkeypatch):
    config_file.write_text(SAMPLE_YAML, encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    assert HarnessSettings().resolved_api_key == "sk-test"


def test_api_key_falls_back_to_provider_env_var(config_file, monkeypatch):
    config_file.write_text("llm:\n  provider: openai\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")

    assert HarnessSettings().resolved_api_key == "sk-openai"


def test_literal_api_key_wins_over_env(config_file, monkeypatch):
    config_file.write_text("llm:\n  provider: deepseek\n  api_key: literal-key\n", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")

    assert HarnessSettings().resolved_api_key == "literal-key"


def test_missing_key_returns_none_instead_of_raising(config_file, monkeypatch):
    """是否「必须有 key」由容器决定，配置层不替实现层做策略。"""
    config_file.write_text("", encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    assert HarnessSettings().resolved_api_key is None


def test_placeholder_from_another_provider_is_rejected(config_file, monkeypatch):
    """切换厂商后不能把上一家的密钥发给新服务。"""
    config_file.write_text(
        "llm:\n  provider: deepseek\n  api_key: ${DEEPSEEK_API_KEY}\n", encoding="utf-8"
    )
    monkeypatch.setenv("HARNESS_LLM__PROVIDER", "openai")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake-deepseek")

    with pytest.raises(ConfigError):
        HarnessSettings().resolved_api_key


def test_switching_provider_uses_that_providers_key(config_file, monkeypatch):
    """留空 api_key 时，密钥跟着 provider 走。"""
    config_file.write_text("llm:\n  provider: deepseek\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_LLM__PROVIDER", "openai")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")

    settings = HarnessSettings()

    assert settings.llm.provider == "openai"
    assert settings.resolved_api_key == "sk-openai"
