"""Provider 适配器测试：请求映射、响应解析、异常翻译、Claude 形状转换。

全部离线：用带 ``chat.completions.create`` / ``messages.create`` 的假客户端。
"""

import json
from types import SimpleNamespace

import pytest

from app.common.api.llm_provider import CompletionRequest, ModelToolCall
from app.common.exception import ConfigError, ProviderError
from app.core.config.settings import ResolvedProvider
from app.core.provider.claude_provider import (
    ClaudeProvider,
    to_anthropic_messages,
    to_anthropic_tools,
)
from app.core.provider.deepseek_provider import DeepSeekProvider
from app.core.provider.openai_provider import OpenAIChatProvider
from app.core.provider.provider_factory import build_provider


# --- OpenAI / DeepSeek ---


class _Completions:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.response


def fake_openai_client(response=None, error=None):
    return SimpleNamespace(chat=SimpleNamespace(completions=_Completions(response, error)))


def openai_response(content=None, tool_calls=(), finish_reason="stop", total_tokens=7, reasoning=None):
    message = SimpleNamespace(
        content=content,
        tool_calls=[
            SimpleNamespace(id=identifier, function=SimpleNamespace(name=name, arguments=arguments))
            for identifier, name, arguments in tool_calls
        ],
        reasoning_content=reasoning,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(total_tokens=total_tokens),
    )


async def test_openai_provider_maps_request_and_response():
    client = fake_openai_client(
        openai_response(
            content="hi",
            tool_calls=[("call-1", "read_file", '{"file_path": "x"}')],
            finish_reason="tool_calls",
            total_tokens=11,
            reasoning="because",
        )
    )
    provider = OpenAIChatProvider(client, model="gpt-test")
    request = CompletionRequest(
        prefix=[{"role": "system", "content": "system"}],
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
    )

    result = await provider.complete(request)

    kwargs = client.chat.completions.kwargs
    assert kwargs["model"] == "gpt-test"
    assert kwargs["messages"][0] == {"role": "system", "content": "system"}
    assert kwargs["messages"][1] == {"role": "user", "content": "hello"}
    assert kwargs["tools"][0]["function"]["name"] == "read_file"
    assert kwargs["tool_choice"] == "auto"
    assert result.content == "hi"
    assert result.finish_reason == "tool_calls"
    assert result.tokens == 11
    assert result.reasoning_content == "because"
    assert result.tool_calls == [
        ModelToolCall(id="call-1", name="read_file", arguments='{"file_path": "x"}')
    ]


async def test_openai_provider_omits_tools_when_empty():
    client = fake_openai_client(openai_response(content="ok"))
    provider = OpenAIChatProvider(client)

    await provider.complete(
        CompletionRequest(prefix=[], messages=[{"role": "user", "content": "hi"}])
    )

    assert "tools" not in client.chat.completions.kwargs


async def test_openai_provider_translates_sdk_errors():
    client = fake_openai_client(error=RuntimeError("boom"))
    provider = OpenAIChatProvider(client)

    with pytest.raises(ProviderError):
        await provider.complete(CompletionRequest(prefix=[], messages=[]))


async def test_deepseek_provider_defaults():
    provider = DeepSeekProvider(fake_openai_client(openai_response(content="ok")))

    assert provider.provider_name == "deepseek"
    assert provider.model == "deepseek-chat"


# --- Claude ---


class _Messages:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.response


def fake_anthropic_client(response=None, error=None):
    return SimpleNamespace(messages=_Messages(response, error))


def claude_response(blocks, stop_reason="end_turn", input_tokens=3, output_tokens=4):
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def test_anthropic_messages_convert_tool_calls_and_results():
    converted = to_anthropic_messages(
        [
            {"role": "user", "content": "read it"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"file_path": "x"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "content"},
            {"role": "tool", "tool_call_id": "t2", "content": "more"},
            {"role": "assistant", "content": "done"},
        ]
    )

    assert converted[0] == {"role": "user", "content": [{"type": "text", "text": "read it"}]}
    assert converted[1]["role"] == "assistant"
    assert converted[1]["content"][0] == {
        "type": "tool_use",
        "id": "t1",
        "name": "read_file",
        "input": {"file_path": "x"},
    }
    # 两条 tool 结果合并成一条 user 消息
    assert converted[2]["role"] == "user"
    assert [block["type"] for block in converted[2]["content"]] == ["tool_result", "tool_result"]
    assert converted[2]["content"][0]["tool_use_id"] == "t1"
    assert converted[3] == {"role": "assistant", "content": [{"type": "text", "text": "done"}]}


def test_anthropic_tools_convert_function_schema():
    converted = to_anthropic_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "read a file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ]
    )

    assert converted == [
        {
            "name": "read_file",
            "description": "read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]


async def test_claude_provider_separates_system_and_parses_tool_use():
    client = fake_anthropic_client(
        claude_response(
            [
                SimpleNamespace(type="text", text="let me read"),
                SimpleNamespace(type="tool_use", id="t1", name="read_file", input={"file_path": "x"}),
            ],
            stop_reason="tool_use",
            input_tokens=5,
            output_tokens=6,
        )
    )
    provider = ClaudeProvider(client, model="claude-test")
    request = CompletionRequest(
        prefix=[{"role": "system", "content": "system rules"}],
        messages=[{"role": "user", "content": "read x"}],
        tools=[{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
    )

    result = await provider.complete(request)

    kwargs = client.messages.kwargs
    assert kwargs["system"] == "system rules"
    assert kwargs["messages"] == [{"role": "user", "content": [{"type": "text", "text": "read x"}]}]
    assert kwargs["tools"][0]["input_schema"] == {"type": "object", "properties": {}}
    assert result.content == "let me read"
    assert result.tokens == 11
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == [
        ModelToolCall(id="t1", name="read_file", arguments=json.dumps({"file_path": "x"}))
    ]


async def test_claude_provider_translates_sdk_errors():
    client = fake_anthropic_client(error=RuntimeError("boom"))
    provider = ClaudeProvider(client)

    with pytest.raises(ProviderError):
        await provider.complete(CompletionRequest(prefix=[], messages=[]))


# --- 工厂 ---


def _resolved(adapter: str) -> ResolvedProvider:
    return ResolvedProvider(
        name="test",
        adapter=adapter,
        api_key="sk-test",
        base_url=None,
        model=None,
        timeout_seconds=30,
        max_retries=1,
    )


def test_factory_builds_each_adapter_with_expected_defaults():
    captured = {}

    def factory(adapter, resolved):
        captured["adapter"] = adapter
        captured["resolved"] = resolved
        return fake_openai_client(openai_response(content="ok"))

    provider = build_provider(_resolved("deepseek"), client_factory=factory)
    assert isinstance(provider, DeepSeekProvider)
    assert captured["adapter"] == "deepseek"
    assert len(provider.model) > 0

    claude = build_provider(
        _resolved("claude"),
        client_factory=lambda adapter, resolved: fake_anthropic_client(claude_response([])),
    )
    assert isinstance(claude, ClaudeProvider)


def test_factory_rejects_unknown_adapter():
    with pytest.raises(ConfigError):
        build_provider(_resolved("bogus"), client_factory=lambda adapter, resolved: None)
