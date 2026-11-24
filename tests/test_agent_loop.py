"""Agent 主循环的行为测试（移植原型 12 条语义，并补审批/恢复/事件）。

全部离线：Provider/工具/上下文/事件都是替身，状态用契约测试的 MemoryStateStore。
"""

import json

import pytest

from app.common.api.approval_service import ApprovalDecision
from app.common.api.llm_provider import ModelResponse, ModelToolCall
from app.common.api.state_store import (
    MessageRole,
    SessionStatus,
    ToolCallStatus,
    TurnStatus,
)
from app.common.exception import ConflictError, ProviderError
from app.core.loop import DefaultAgentLoop, LoopConfig, TurnEngine
from tests.fakes.fake_context_events import PassThroughContext, RecordingEventPublisher
from tests.fakes.fake_provider import ScriptedProvider
from tests.fakes.fake_tools import FakeApprovalService, FakeToolRegistry
from tests.fakes.memory_audit_service import MemoryAuditService
from tests.fakes.memory_state_store import MemoryStateStore


def call(call_id: str, name: str, arguments: dict | str) -> ModelToolCall:
    if isinstance(arguments, str):
        return ModelToolCall(id=call_id, name=name, arguments=arguments)
    return ModelToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


def response(*calls, content=None, finish_reason=None, tokens=0, reasoning_content=None):
    return ModelResponse(
        content=content,
        tool_calls=list(calls),
        finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
        tokens=tokens,
        reasoning_content=reasoning_content,
    )


class Harness:
    def __init__(
        self,
        responses,
        *,
        sensitive=(),
        max_iterations=15,
        context=None,
        events=None,
    ):
        self.state = MemoryStateStore()
        self.provider = ScriptedProvider(list(responses))
        self.tools = FakeToolRegistry()
        self.context = context or PassThroughContext()
        self.events = events or RecordingEventPublisher()
        self.approval = FakeApprovalService(self.state, sensitive)
        self.audit = MemoryAuditService(self.state)
        self.engine = TurnEngine(
            provider=self.provider,
            tools=self.tools,
            state=self.state,
            context=self.context,
            approval=self.approval,
            events=self.events,
            config=LoopConfig(max_iterations=max_iterations),
        )
        self.loop = DefaultAgentLoop(self.engine, state=self.state, audit=self.audit)

    async def session(self):
        return await self.state.create_session(title="test")


# --- 基础循环 ---


async def test_loop_preserves_history_and_final_reply():
    harness = Harness(
        [
            response(
                call("read", "read_file", {"file_path": "text.txt"}),
                call("list", "list_files", {"directory": "."}),
                tokens=10,
                reasoning_content="thinking",
            ),
            response(content="Complete", tokens=5),
        ]
    )
    harness.tools.add("read_file", lambda arguments: "real file content")
    harness.tools.add("list_files", lambda arguments: ["a.txt"])
    session = await harness.session()

    outcome = await harness.loop.run_turn(session.id, user_content="Inspect files")

    assert outcome.status == TurnStatus.COMPLETED
    assert outcome.content == "Complete"
    assert outcome.tokens == 15
    assert outcome.iterations == 2

    messages = await harness.state.list_messages(session.id)
    assert [message.role for message in messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    assert messages[-1].content == "Complete"

    second_request = harness.provider.requests[1].messages
    assert second_request[0]["role"] == "user"
    assert [item["id"] for item in second_request[1]["tool_calls"]] == ["read", "list"]
    assert [item["role"] for item in second_request[2:]] == ["tool", "tool"]
    assert second_request[2]["tool_call_id"] == "read"
    assert "real file content" in second_request[2]["content"]

    assert harness.context.records == [10, 5]
    assert "turn_completed" in harness.events.kinds()
    assert [event.payload["call_id"] for event in harness.events.events if str(event.type) == "tool_end"] == [
        "read",
        "list",
    ]


async def test_invalid_json_and_unknown_tools_return_linked_errors():
    harness = Harness(
        [
            response(
                call("bad-json", "read_file", "{"),
                call("bad-type", "read_file", "[]"),
                call("unknown", "no_such_tool", {}),
                call("good", "read_file", {"file_path": "x"}),
            ),
            response(content="Errors observed"),
        ]
    )
    harness.tools.add("read_file", lambda arguments: "ok")
    session = await harness.session()

    outcome = await harness.loop.run_turn(session.id, user_content="Inspect")

    assert outcome.status == TurnStatus.COMPLETED
    tool_messages = [
        message for message in await harness.state.list_messages(session.id) if message.role == MessageRole.TOOL
    ]
    assert [message.tool_call_id for message in tool_messages] == [
        "bad-json",
        "bad-type",
        "unknown",
        "good",
    ]
    payloads = [json.loads(message.content) for message in tool_messages]
    assert [payload["status"] for payload in payloads] == ["error", "error", "error", "success"]


async def test_provider_error_fails_the_turn():
    harness = Harness([ProviderError("upstream down")])
    session = await harness.session()

    outcome = await harness.loop.run_turn(session.id, user_content="hi")

    assert outcome.status == TurnStatus.FAILED
    assert "ProviderError" in (outcome.error or "")
    assert (await harness.state.load_session(session.id)).status == SessionStatus.FAILED.value


# --- 审批：挂起 / 批准 / 拒绝 ---


async def test_sensitive_call_suspends_without_executing():
    harness = Harness(
        [response(call("write", "write_file", {"file_path": "new.txt"}))],
        sensitive={"write_file"},
    )
    harness.tools.add("write_file", lambda arguments: {"written": True})
    session = await harness.session()

    outcome = await harness.loop.run_turn(session.id, user_content="Write")

    assert outcome.status == TurnStatus.WAITING_APPROVAL
    assert outcome.pending_call_id == "write"
    assert harness.tools.executed == []
    assert (await harness.state.load_session(session.id)).status == SessionStatus.WAITING_APPROVAL.value
    assert [event.type for event in harness.events.events if str(event.type) == "approval_required"]


async def test_approved_call_executes_on_resume():
    harness = Harness(
        [
            response(call("write", "write_file", {"file_path": "new.txt"})),
            response(content="Edited and verified"),
        ],
        sensitive={"write_file"},
    )
    harness.tools.add("write_file", lambda arguments: {"written": True})
    session = await harness.session()
    await harness.loop.run_turn(session.id, user_content="Write")

    approval = await harness.state.get_approval_for_call("write")
    await harness.approval.decide(approval.id, ApprovalDecision.APPROVED, actor="teacher")
    outcome = await harness.loop.resume_turn(session.id, approval_id=approval.id)

    assert outcome.status == TurnStatus.COMPLETED
    assert outcome.content == "Edited and verified"
    assert harness.tools.executed == [("write_file", {"file_path": "new.txt"})]
    assert "approval_decided" in harness.events.kinds()


async def test_denial_stops_remaining_calls_and_future_model_turns():
    harness = Harness(
        [
            response(
                call("write", "write_file", {"file_path": "new.txt"}),
                call("read", "read_file", {"file_path": "text.txt"}),
            )
        ],
        sensitive={"write_file"},
    )
    harness.tools.add("write_file", lambda arguments: pytest.fail("refused tool must not run"))
    harness.tools.add("read_file", lambda arguments: "content")
    session = await harness.session()
    await harness.loop.run_turn(session.id, user_content="Write")

    approval = await harness.state.get_approval_for_call("write")
    await harness.approval.decide(approval.id, ApprovalDecision.DENIED, actor="teacher")
    outcome = await harness.loop.resume_turn(session.id, approval_id=approval.id)

    assert outcome.status == TurnStatus.FAILED
    assert outcome.error == "Tool approval denied"
    assert harness.tools.executed == []
    assert len(harness.provider.requests) == 1  # 拒绝后不再请求模型

    calls = {row.id: row for row in await harness.state.list_tool_calls(session.id)}
    assert calls["write"].status == ToolCallStatus.DENIED.value
    assert calls["read"].status == ToolCallStatus.DENIED.value
    assert "tool_skipped" in harness.events.kinds()


async def test_resume_rejects_undecided_or_foreign_approval():
    harness = Harness([response(call("write", "write_file", {}))], sensitive={"write_file"})
    harness.tools.add("write_file", lambda arguments: {"written": True})
    session = await harness.session()
    await harness.loop.run_turn(session.id, user_content="Write")

    with pytest.raises(ConflictError):
        await harness.loop.resume_turn(session.id, approval_id="does-not-exist")


async def test_resume_without_open_turn_raises_conflict():
    harness = Harness([])
    session = await harness.session()

    with pytest.raises(ConflictError):
        await harness.loop.resume_turn(session.id)


# --- 不完整响应 / 非法调用 id / 迭代上限 ---


async def test_incomplete_response_does_not_execute_tools():
    for reason in ("length", "content_filter"):
        harness = Harness(
            [response(call("read", "read_file", {"file_path": "x"}), finish_reason=reason)]
        )
        harness.tools.add("read_file", lambda arguments: "content")
        session = await harness.session()

        outcome = await harness.loop.run_turn(session.id, user_content="Inspect")

        assert outcome.status == TurnStatus.FAILED
        assert outcome.error == "Incomplete model response"
        assert harness.tools.executed == []


async def test_duplicate_call_ids_do_not_execute():
    harness = Harness(
        [response(call("same", "read_file", {"file_path": "x"}), call("same", "read_file", {"file_path": "x"}))]
    )
    harness.tools.add("read_file", lambda arguments: "content")
    session = await harness.session()

    outcome = await harness.loop.run_turn(session.id, user_content="Inspect")

    assert outcome.status == TurnStatus.FAILED
    assert harness.tools.executed == []


async def test_reused_call_id_is_not_replayed():
    harness = Harness(
        [
            response(call("same", "read_file", {"file_path": "x"})),
            response(call("same", "read_file", {"file_path": "x"})),
        ]
    )
    harness.tools.add("read_file", lambda arguments: "content")
    session = await harness.session()

    outcome = await harness.loop.run_turn(session.id, user_content="Inspect")

    assert outcome.status == TurnStatus.FAILED
    assert len(harness.tools.executed) == 1


async def test_iteration_limit():
    harness = Harness(
        [response(call("read", "read_file", {"file_path": "x"}))], max_iterations=1
    )
    harness.tools.add("read_file", lambda arguments: "content")
    session = await harness.session()

    outcome = await harness.loop.run_turn(session.id, user_content="Inspect")

    assert outcome.status == TurnStatus.FAILED
    assert "Maximum iterations" in (outcome.error or "")
    assert outcome.iterations == 1
    assert len(harness.tools.executed) == 1


def test_invalid_iteration_limit_is_rejected():
    for limit in (0, -1):
        with pytest.raises(ValueError):
            LoopConfig(max_iterations=limit)


# --- 事件 best-effort / 脱敏 ---


async def test_event_sink_failure_does_not_fail_the_turn():
    harness = Harness([response(content="done")], events=RecordingEventPublisher(fail=True))
    session = await harness.session()

    outcome = await harness.loop.run_turn(session.id, user_content="hi")

    assert outcome.status == TurnStatus.COMPLETED


async def test_events_are_redacted_but_history_keeps_content(monkeypatch):
    secret = "sk-verysecretvalue123456"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    harness = Harness([response(content=secret)])
    session = await harness.session()

    await harness.loop.run_turn(session.id, user_content=secret)

    messages = await harness.state.list_messages(session.id)
    assert messages[-1].content == secret  # 原始历史保留
    assert secret not in json.dumps([event.to_dict() for event in harness.events.events])


# --- 恢复 ---


async def test_unknown_dispatch_is_reported_not_replayed():
    harness = Harness([])
    harness.tools.add("read_file", lambda arguments: "content")
    session = await harness.session()
    await harness.state.append_message(session.id, MessageRole.USER, "go")
    await harness.state.record_assistant_turn(
        session.id, response(call("c1", "read_file", {"file_path": "x"}))
    )
    await harness.state.claim_tool_call(
        "c1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
    )

    outcome = await harness.loop.run_turn(session.id)

    assert outcome.status == TurnStatus.FAILED
    assert "unknown" in (outcome.error or "")
    failed = [event for event in harness.events.events if str(event.type) == "turn_failed"]
    assert failed[-1].payload["unknown_calls"] == ["c1"]
    assert harness.tools.executed == []


async def test_pending_call_is_resumed_before_next_model_step():
    harness = Harness([response(content="finished")])
    harness.tools.add("read_file", lambda arguments: "content")
    session = await harness.session()
    await harness.state.append_message(session.id, MessageRole.USER, "go")
    await harness.state.record_assistant_turn(
        session.id, response(call("c1", "read_file", {"file_path": "x"}))
    )

    outcome = await harness.loop.run_turn(session.id)

    assert outcome.status == TurnStatus.COMPLETED
    assert harness.tools.executed == [("read_file", {"file_path": "x"})]
    assert len(harness.provider.requests) == 1


async def test_summary_injected_mid_turn_keeps_order():
    harness = Harness(
        [response(call("read", "read_file", {"file_path": "x"})), response(content="done")],
        context=PassThroughContext(compact_after_first_call=True),
    )
    harness.tools.add("read_file", lambda arguments: "content")
    session = await harness.session()

    outcome = await harness.loop.run_turn(session.id, user_content="Inspect")

    assert outcome.status == TurnStatus.COMPLETED
    messages = await harness.state.list_messages(session.id)
    assert any(message.role == MessageRole.SUMMARY for message in messages)
    first_request = harness.provider.requests[0].messages
    assert any("[历史摘要]" in str(message.get("content")) for message in first_request)


async def test_abandon_turn_expires_approvals_and_writes_audit():
    harness = Harness([response(call("write", "write_file", {}))], sensitive={"write_file"})
    harness.tools.add("write_file", lambda arguments: {"written": True})
    session = await harness.session()
    await harness.loop.run_turn(session.id, user_content="Write")

    outcome = await harness.loop.abandon_turn(session.id, reason="user cancelled")

    assert outcome.status == TurnStatus.ABANDONED
    assert (await harness.state.load_session(session.id)).status == SessionStatus.ABANDONED.value
    assert await harness.state.list_pending_approvals(session.id) == []
    audits = await harness.audit.query(session_id=session.id)
    assert any(row.action == "turn_abandoned" for row in audits)
    assert any(row.action == "approval_expired" for row in audits)


async def test_abandoning_a_finished_turn_is_rejected():
    harness = Harness([response(content="done")])
    session = await harness.session()
    await harness.loop.run_turn(session.id, user_content="hi")

    with pytest.raises(ConflictError):
        await harness.loop.abandon_turn(session.id)
