# 来源：D:/Programs/Python/OOAD/Assignment1/agent.py:run_agent 消息流及同目录 support/cases.py 验证思想。
# 功能：用 FakeProvider 验证多轮、多工具、错误、拒绝、脱敏和事件；不使用付费 API。
# 后续：加入重启恢复、上下文预算、要求记忆覆盖以及 SSE/数据库集成测试。
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.common.event.runtime import MemoryEventSink
from app.common.model.runtime import ModelResponse, ToolCall
from app.core.agent import Agent
from app.core.provider import FakeProvider
from app.tools.registry import create_builtin_registry


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="harness agent ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "text.txt").write_text("real file content", encoding="utf-8")
        self.registry = create_builtin_registry(self.root)

    def test_loop_preserves_history_and_final_reply(self):
        provider = FakeProvider([
            ModelResponse(tool_calls=[ToolCall("read", "read_file", '{"file_path":"text.txt"}'),
                                      ToolCall("list", "list_files", '{"directory":"."}')],
                          finish_reason="tool_calls", tokens=10, reasoning_content="reasoning"),
            ModelResponse(content="Complete", tokens=5),
        ])
        sink = MemoryEventSink()
        result = Agent(provider, self.registry, event_sink=sink).run_agent("Inspect files")
        self.assertTrue(result["success"])
        self.assertEqual(result["tokens"], 15)
        self.assertEqual(result["tool_calls"], 2)
        history = provider.requests[1]["messages"]
        self.assertEqual([m["role"] for m in history], ["system", "user", "assistant", "tool", "tool"])
        self.assertEqual([m["tool_call_id"] for m in history[3:]], ["read", "list"])
        self.assertEqual(history[2]["reasoning_content"], "reasoning")
        self.assertEqual(json.loads(history[3]["content"])["result"], "real file content")
        self.assertEqual(result["messages"][-1], {"role": "assistant", "content": "Complete"})
        self.assertEqual(sink.events, result["trace"])
        self.assertEqual([event["seq"] for event in sink.events], list(range(1, len(sink.events) + 1)))

    def test_invalid_json_and_unknown_tools_return_linked_errors(self):
        provider = FakeProvider([
            ModelResponse(tool_calls=[ToolCall("bad-json", "read_file", "{"),
                                      ToolCall("bad-type", "read_file", "[]"),
                                      ToolCall("unknown", "no_such_tool", "{}")]),
            ModelResponse(content="Errors observed"),
        ])
        result = Agent(provider, self.registry).run_agent("Inspect")
        self.assertTrue(result["success"])
        outputs = provider.requests[1]["messages"][3:]
        self.assertEqual([m["tool_call_id"] for m in outputs], ["bad-json", "bad-type", "unknown"])
        self.assertTrue(all(json.loads(m["content"])["status"] == "error" for m in outputs))

    def test_authorized_read_edit_run_vertical_slice(self):
        class Approval:
            def approve(self, request):
                return True
        registry = create_builtin_registry(self.root, Approval())
        (self.root / "program.py").write_text('print("before")\n', encoding="utf-8")
        provider = FakeProvider([
            ModelResponse(tool_calls=[ToolCall("read", "read_file", '{"file_path":"program.py"}')]),
            ModelResponse(tool_calls=[ToolCall("write", "write_file", json.dumps({"file_path": "program.py", "content": 'print("after")\n'}))]),
            ModelResponse(tool_calls=[ToolCall("run", "run_python", '{"file_path":"program.py"}')]),
            ModelResponse(content="Edited and verified"),
        ])
        result = Agent(provider, registry).run_agent("Read, edit and verify program.py")
        self.assertTrue(result["success"])
        self.assertEqual(result["tool_calls"], 3)
        actual = json.loads(provider.requests[-1]["messages"][-1]["content"])
        self.assertEqual(actual["result"], {"returncode": 0, "stdout": "after\n", "stderr": ""})

    def test_denial_stops_remaining_calls_and_future_model_turns(self):
        provider = FakeProvider([ModelResponse(tool_calls=[
            ToolCall("write", "write_file", '{"file_path":"new.txt","content":"denied"}'),
            ToolCall("read", "read_file", '{"file_path":"text.txt"}'),
        ])])
        result = Agent(provider, self.registry).run_agent("Write")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Tool approval denied")
        self.assertEqual(result["tool_calls"], 1)
        self.assertFalse((self.root / "new.txt").exists())
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual([m["tool_call_id"] for m in result["messages"][-2:]], ["write", "read"])
        self.assertTrue(any(event["kind"] == "tool_skipped" for event in result["trace"]))

    def test_incomplete_response_does_not_execute_tools(self):
        for reason in ("length", "content_filter"):
            provider = FakeProvider([ModelResponse(tool_calls=[ToolCall("read", "read_file", '{"file_path":"text.txt"}')], finish_reason=reason)])
            with self.subTest(reason=reason):
                result = Agent(provider, self.registry).run_agent("Inspect")
                self.assertFalse(result["success"])
                self.assertEqual(result["tool_calls"], 0)

    def test_duplicate_call_ids_do_not_execute(self):
        provider = FakeProvider([ModelResponse(tool_calls=[ToolCall("same", "read_file", "{}"), ToolCall("same", "read_file", "{}")])])
        result = Agent(provider, self.registry).run_agent("Inspect")
        self.assertFalse(result["success"])
        self.assertEqual(result["tool_calls"], 0)

    def test_reused_call_id_is_not_replayed(self):
        provider = FakeProvider([
            ModelResponse(tool_calls=[ToolCall("same", "read_file", '{"file_path":"text.txt"}')]),
            ModelResponse(tool_calls=[ToolCall("same", "read_file", '{"file_path":"text.txt"}')]),
        ])
        result = Agent(provider, self.registry).run_agent("Inspect")
        self.assertFalse(result["success"])
        self.assertEqual(result["tool_calls"], 1)

    def test_invalid_iteration_limit(self):
        for limit in (0, -1, True):
            with self.subTest(limit=limit):
                provider = FakeProvider([])
                result = Agent(provider, self.registry).run_agent("Inspect", max_iterations=limit)
                self.assertFalse(result["success"])
                self.assertEqual(provider.requests, [])

    def test_iteration_limit(self):
        provider = FakeProvider([ModelResponse(tool_calls=[ToolCall("read", "read_file", '{"file_path":"text.txt"}')])])
        result = Agent(provider, self.registry).run_agent("Inspect", max_iterations=1)
        self.assertFalse(result["success"])
        self.assertIn("Maximum iterations", result["error"])
        self.assertEqual(result["tool_calls"], 1)

    def test_events_are_redacted_but_model_history_retains_original(self):
        secret = "private-provider-credential"
        provider = FakeProvider([ModelResponse(content=secret)])
        sink = MemoryEventSink()
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}):
            result = Agent(provider, self.registry, event_sink=sink).run_agent(secret)
        self.assertNotIn(secret, json.dumps(result["trace"]))
        self.assertNotIn(secret, json.dumps(sink.events))
        self.assertEqual(result["messages"][-1]["content"], secret)

    def test_event_failure_blocks_further_work(self):
        class BrokenSink:
            def publish(self, event):
                raise RuntimeError("event store unavailable")
        provider = FakeProvider([ModelResponse(content="unused")])
        result = Agent(provider, self.registry, event_sink=BrokenSink()).run_agent("Inspect")
        self.assertFalse(result["success"])
        self.assertEqual(provider.requests, [])
        self.assertIn("Event delivery failed", result["error"])