# 来源：D:/Programs/Python/OOAD/Assignment1/agent.py:main（349–356 行）及注入方式（245–252 行）。
# 功能：用 CLI 临时组装 Provider、Agent、工具和审批；提供不联网的 --demo，不调用作业评分器。
# 后续：由 Web 启动层替换并接入 REST/SSE；当前入口不是完整 FastAPI 服务，不支持持久化会话。
import argparse
import json
import tempfile
from pathlib import Path

from app.common.event.runtime import MemoryEventSink
from app.common.model.runtime import ModelResponse, ToolCall
from app.common.security import redact_value
from app.core.agent import Agent
from app.core.provider import DEFAULT_MODEL, FakeProvider, OpenAIProvider, create_client
from app.tools.approval import DenyApproval, TerminalApproval
from app.tools.registry import create_builtin_registry


def demo():
    with tempfile.TemporaryDirectory(prefix="toy-harness-demo-") as directory:
        (Path(directory) / "hello.txt").write_text("Hello from the workspace.\n", encoding="utf-8")
        provider = FakeProvider([
            ModelResponse(tool_calls=[ToolCall("demo-read", "read_file", '{"file_path": "hello.txt"}')],
                          finish_reason="tool_calls"),
            ModelResponse(content="Demo completed using the read_file tool."),
        ])
        sink = MemoryEventSink()
        result = Agent(provider, create_builtin_registry(directory), event_sink=sink).run_agent("Read hello.txt")
        print(json.dumps(redact_value(result), ensure_ascii=False, indent=2))
        return 0 if result["success"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Toy Harness: transplanted Nano Codex CLI prototype")
    parser.add_argument("query", nargs="?")
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1] / "workspace"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-iterations", type=int, default=15)
    parser.add_argument("--interactive-approval", action="store_true")
    parser.add_argument("--demo", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.demo:
        return demo()
    if not arguments.query:
        parser.error("provide a query or --demo")
    approval = TerminalApproval() if arguments.interactive_approval else DenyApproval()
    try:
        registry = create_builtin_registry(arguments.workspace, approval)
        with create_client() as client:
            result = Agent(OpenAIProvider(client, arguments.model), registry).run_agent(
                arguments.query, max_iterations=arguments.max_iterations)
    except Exception as error:
        print(json.dumps(redact_value({"success": False, "error": f"{type(error).__name__}: {error}"}), ensure_ascii=False))
        return 2
    print(json.dumps(redact_value(result), ensure_ascii=False, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
