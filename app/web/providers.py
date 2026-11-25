"""Provider selection for the local Web entry point."""

import json
import os
import time
from contextlib import contextmanager
from typing import Iterator, Literal

from openai import OpenAI

from app.common.exception import ConfigError
from app.common.model.runtime import ModelResponse, ToolCall
from app.core.config.settings import load_env
from app.core.provider import DEFAULT_MODEL, OpenAIProvider


ProviderName = Literal["demo", "deepseek-flash"]
DEMO_PROMPT = "读取 notes.txt，把「待讨论」改成「已确认」。"
DEMO_INITIAL_CONTENT = "待讨论\n"
DEMO_WRITTEN_CONTENT = "已确认\n"


class DemoProvider:
    def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        # Keep the model-request state visible in the scripted classroom run.
        time.sleep(0.45)
        results = [message["content"] for message in messages if message["role"] == "tool"]
        if results and json.loads(results[-1])["status"] != "success":
            return ModelResponse(content="演示工具执行失败，请查看执行时间线。")
        if not results:
            call = ToolCall("demo-read", "read_file", json.dumps({"file_path": "notes.txt"}))
        elif len(results) == 1:
            arguments = {"file_path": "notes.txt", "content": DEMO_WRITTEN_CONTENT}
            call = ToolCall("demo-write", "write_file", json.dumps(arguments, ensure_ascii=False))
        else:
            return ModelResponse(content="已将 notes.txt 中的「待讨论」改成「已确认」。")
        return ModelResponse(tool_calls=[call], finish_reason="tool_calls")


def provider_name() -> ProviderName:
    configured = os.getenv("TOY_HARNESS_PROVIDER", "demo")
    if configured == "demo":
        return "demo"
    if configured == "deepseek-flash":
        return "deepseek-flash"
    raise ConfigError("TOY_HARNESS_PROVIDER 必须设为 demo 或 deepseek-flash")


@contextmanager
def open_provider(name: ProviderName) -> Iterator[DemoProvider | OpenAIProvider]:
    if name == "demo":
        yield DemoProvider()
        return

    load_env()
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise ConfigError("请设置 DEEPSEEK_API_KEY，或使用离线演示模式")
    with OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        timeout=35.0,
        max_retries=0,
    ) as client:
        yield OpenAIProvider(client, DEFAULT_MODEL)
