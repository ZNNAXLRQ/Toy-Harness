import json

import pytest

from app.common.exception import ConfigError


def test_demo_provider_only_proposes_writing_after_successful_read(monkeypatch):
    from app.web.providers import DEMO_WRITTEN_CONTENT, open_provider, provider_name, time

    monkeypatch.delenv("TOY_HARNESS_PROVIDER", raising=False)
    monkeypatch.setattr(time, "sleep", lambda delay: None)
    assert provider_name() == "demo"
    with open_provider("demo") as provider:
        read = provider.complete([], [])
        assert read.tool_calls[0].name == "read_file"
        result = {"role": "tool", "content": json.dumps({"status": "success", "result": "old"})}
        write = provider.complete([result], [])
        assert write.tool_calls[0].name == "write_file"
        assert json.loads(write.tool_calls[0].arguments)["content"] == DEMO_WRITTEN_CONTENT
        assert not provider.complete([result, result], []).tool_calls
        result["content"] = json.dumps({"status": "error", "result": "file not found"})
        assert not provider.complete([result], []).tool_calls


def test_live_provider_requires_configured_key(monkeypatch):
    from app.web import providers

    monkeypatch.setenv("TOY_HARNESS_PROVIDER", "deepseek-flash")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(providers, "load_env", lambda: None)
    assert providers.provider_name() == "deepseek-flash"
    with pytest.raises(ConfigError, match="DEEPSEEK_API_KEY"):
        with providers.open_provider("deepseek-flash"):
            pytest.fail("Started without credentials")


def test_live_provider_uses_flash_on_the_official_endpoint(monkeypatch):
    from app.web import providers

    class LocalClient:
        def __init__(self, **configuration):
            assert configuration["api_key"] == "fixture-value"
            assert configuration["base_url"] == "https://api.deepseek.com"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-value")
    monkeypatch.setattr(providers, "OpenAI", LocalClient)
    with providers.open_provider("deepseek-flash") as provider:
        assert provider.model == "deepseek-flash"
