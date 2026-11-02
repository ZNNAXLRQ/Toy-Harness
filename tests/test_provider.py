# 来源：D:/Programs/Python/OOAD/Assignment1/agent.py 的可注入 client、URL 校验和响应解析（227–299 行）。
# 功能：验证适配器、配置隔离和 CLI 离线演示，不读取课程密钥、不发网络请求。
# 后续：补充真实供应商兼容性、流式中断和超时恢复测试。
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from app.core.provider import OpenAIProvider, create_client, normalize_base_url
from app.core.agent import Agent
from app.main import main
from app.tools.registry import create_builtin_registry


class ProviderTests(unittest.TestCase):
    def test_base_url_validation(self):
        self.assertEqual(normalize_base_url("https://example.invalid/"), "https://example.invalid/v1")
        self.assertEqual(normalize_base_url("https://example.invalid/custom/v1"), "https://example.invalid/custom/v1")
        for value in ("localhost", "ftp://example.invalid", "https://user:secret@example.invalid", "https://example.invalid?token=x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_base_url(value)

    def test_sdk_adapter_preserves_usage_calls_and_reasoning(self):
        message = NS(content=None, tool_calls=[NS(id="call", function=NS(name="read_file", arguments='{"file_path":"x"}'))], reasoning_content="reasoning")
        response = NS(choices=[NS(message=message, finish_reason="tool_calls")], usage=NS(total_tokens=12))
        client = Mock()
        client.chat.completions.create.return_value = response
        provider = OpenAIProvider(client, model="test-model")
        result = provider.complete([{"role": "user", "content": "read"}], [{"type": "function"}])
        self.assertEqual(result.tokens, 12)
        self.assertEqual(result.tool_calls[0].name, "read_file")
        self.assertEqual(result.reasoning_content, "reasoning")
        self.assertEqual(client.chat.completions.create.call_args.kwargs["model"], "test-model")

    def test_empty_model_choices_raise(self):
        client = Mock()
        client.chat.completions.create.return_value = NS(choices=[])
        with self.assertRaises(ValueError):
            OpenAIProvider(client).complete([], [])

    def test_missing_or_placeholder_configuration_is_rejected(self):
        for values in ({}, {"BASE_URL": "https://example.invalid", "OPENAI_API_KEY": "replace-with-your-own-key"}):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True), patch("dotenv.load_dotenv"), self.assertRaises(ValueError):
                create_client()

    def test_demo_uses_no_live_client(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("app.main.create_client", side_effect=AssertionError("Unexpected live API")):
            code = main(["--demo"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(result["success"])
        self.assertEqual(result["tool_calls"], 1)

    def test_real_sdk_serializes_messages_over_mock_transport(self):
        import httpx
        from openai import OpenAI

        requests = []
        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                message = {"role": "assistant", "content": None, "reasoning_content": "provider reasoning",
                           "tool_calls": [{"id": "sdk-read", "type": "function", "function": {
                               "name": "read_file", "arguments": '{"file_path":"text.txt"}'}}]}
            else:
                history = body["messages"]
                self.assertEqual(history[2]["reasoning_content"], "provider reasoning")
                self.assertEqual(history[-1]["tool_call_id"], "sdk-read")
                self.assertEqual(json.loads(history[-1]["content"])["result"], "actual file")
                message = {"role": "assistant", "content": "Complete"}
            return httpx.Response(200, json={"id": "mock", "object": "chat.completion", "created": 0,
                "model": "test-model", "choices": [{"index": 0, "message": message,
                "finish_reason": "tool_calls" if len(requests) == 1 else "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

        with tempfile.TemporaryDirectory(prefix="harness sdk ") as directory:
            (Path(directory) / "text.txt").write_text("actual file", encoding="utf-8")
            with OpenAI(api_key="offline-placeholder", base_url="https://offline.invalid/v1",
                        http_client=httpx.Client(transport=httpx.MockTransport(handle)), max_retries=0) as client:
                result = Agent(OpenAIProvider(client, "test-model"), create_builtin_registry(directory)).run_agent("Read")
        self.assertTrue(result["success"])
        self.assertEqual(len(requests), 2)
        self.assertEqual(result["tokens"], 4)