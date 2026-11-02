# 来源：D:/Programs/Python/OOAD/Assignment1/agent.py:normalize_base_url/create_client 及响应解析（227–299 行）。
# 功能：保留 OpenAI-compatible 调用、超时、usage 和 reasoning_content，增加独立适配器与假 Provider。
# 后续：异步/流式响应、输出预算和其他供应商；代理沿用进程环境，不硬编码课程地址。
import os
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from app.common.model.runtime import ModelResponse, ToolCall

DEFAULT_MODEL = "deepseek-flash"


def normalize_base_url(value):
    value = value.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("BASE_URL must be an absolute HTTP(S) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("BASE_URL must not contain credentials, a query or a fragment")
    return value if parts.path else value + "/v1"


def create_client(env_file=None):
    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv(env_file or Path(__file__).resolve().parents[2] / ".env")
    api_key, base_url = os.getenv("OPENAI_API_KEY"), os.getenv("BASE_URL")
    if not api_key or api_key == "replace-with-your-own-key" or not base_url:
        raise ValueError("Configure OPENAI_API_KEY and BASE_URL in the process environment or Project/.env")
    return OpenAI(api_key=api_key, base_url=normalize_base_url(base_url), timeout=60.0, max_retries=0)


class OpenAIProvider:
    def __init__(self, client, model=DEFAULT_MODEL):
        self.client, self.model = client, model

    def complete(self, messages, tools):
        arguments = {"model": self.model, "messages": messages}
        if tools:
            arguments.update(tools=tools, tool_choice="auto")
        response = self.client.chat.completions.create(**arguments)
        if not response.choices:
            raise ValueError("Model returned no choices")
        choice = response.choices[0]
        message = choice.message
        return ModelResponse(
            content=message.content,
            tool_calls=[ToolCall(call.id, call.function.name, call.function.arguments)
                        for call in message.tool_calls or []],
            finish_reason=choice.finish_reason,
            tokens=getattr(getattr(response, "usage", None), "total_tokens", 0) or 0,
            reasoning_content=getattr(message, "reasoning_content", None),
        )


class FakeProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
        try:
            return next(self.responses)
        except StopIteration as error:
            raise RuntimeError("FakeProvider has no more scripted responses") from error