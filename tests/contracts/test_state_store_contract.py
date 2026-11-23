"""StateStore 端口契约。

这是「可执行的规格」：内存替身、C 的 SQLite 实现、将来的其它后端都必须原样通过。
新增实现时把它加进 ``store`` 的 params 即可，测试本身不改。

覆盖的几类不变式（每一类都来自一次真实评审）：
- 消息 ``seq`` 单调、按会话独立；
- assistant 消息与 tool_calls **一次写入**，冲突时不留半条记录；
- 工具调用状态机（非法迁移/终态迁出直接拒绝）；
- 条件派发（同一调用只会被一方 claim 成功）；
- 审批拒绝可以写入（工具从未派发），派发后的结果不能改判为拒绝；
- 补偿凭据可读回；
- 恢复：四种中断都能接续、UNKNOWN 幂等、``abandoned`` 不复活、``summary`` 不重开；
- 事务回滚；结果序列化失败不落地半个结果。
"""

import pytest

from app.common.api.llm_provider import ModelResponse, ModelToolCall
from app.common.api.state_store import (
    MessageRole,
    SessionStatus,
    ToolCallStatus,
    TurnStatus,
)
from app.common.api.tool import ToolResult
from app.common.exception import NotFoundError, StateError
from tests.fakes.memory_state_store import MemoryStateStore


@pytest.fixture(params=["memory", "sqlite"])
async def store(request):
    if request.param == "memory":
        return MemoryStateStore()
    pytest.skip(
        "C 的 SQLiteStateStore 尚未交付；交付后在 tests/contracts 里构造真实实现即可复用本套测试"
    )


def _response(*call_ids, content=None):
    return ModelResponse(
        content=content,
        tool_calls=[
            ModelToolCall(id=identifier, name="write_file", arguments="{}")
            for identifier in call_ids
        ],
    )


async def _turn_with_calls(store, *call_ids):
    session = await store.create_session(title="contract")
    await store.append_message(session.id, MessageRole.USER, "do it")
    await store.record_assistant_turn(session.id, _response(*call_ids))
    return session


# --- 消息 ---


async def test_message_seq_is_per_session_and_starts_at_one(store):
    first = await store.create_session(title="a")
    second = await store.create_session(title="b")

    await store.append_message(first.id, MessageRole.USER, "hello")
    await store.append_message(first.id, MessageRole.ASSISTANT, "hi")
    await store.append_message(second.id, MessageRole.USER, "other")

    assert [m.seq for m in await store.list_messages(first.id)] == [1, 2]
    assert [m.seq for m in await store.list_messages(second.id)] == [1]


async def test_list_messages_after_seq_returns_tail_in_order(store):
    session = await store.create_session()
    for index in range(3):
        await store.append_message(session.id, MessageRole.USER, f"m{index}")

    tail = await store.list_messages(session.id, after_seq=1)
    assert [m.content for m in tail] == ["m1", "m2"]


async def test_assistant_turn_writes_message_and_calls_together(store):
    session = await _turn_with_calls(store, "call-1", "call-2")

    messages = await store.list_messages(session.id)
    assert [m.role for m in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assistant = messages[-1]

    calls = await store.list_tool_calls(session.id)
    assert [c.id for c in calls] == ["call-1", "call-2"]
    assert all(c.message_id == assistant.id for c in calls)
    assert all(c.status == ToolCallStatus.PENDING.value for c in calls)


async def test_assistant_turn_with_reused_call_id_leaves_no_message(store):
    session = await _turn_with_calls(store, "call-1")
    before = len(await store.list_messages(session.id))

    with pytest.raises(StateError):
        await store.record_assistant_turn(session.id, _response("call-1"))

    assert len(await store.list_messages(session.id)) == before


async def test_duplicate_call_ids_inside_one_turn_are_rejected(store):
    session = await store.create_session()

    with pytest.raises(StateError):
        await store.record_assistant_turn(session.id, _response("same", "same"))


# --- 状态机 ---


async def test_illegal_transition_is_rejected(store):
    session = await _turn_with_calls(store, "call-1")
    await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
    )
    await store.finish_tool_call("call-1", ToolResult("success", "ok"))

    with pytest.raises(StateError):
        await store.set_tool_status("call-1", ToolCallStatus.DISPATCHED)


async def test_only_one_claimant_can_dispatch_a_call(store):
    await _turn_with_calls(store, "call-1")

    first = await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
    )
    second = await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
    )

    assert first is True
    assert second is False


async def test_claim_rejects_illegal_target_state(store):
    await _turn_with_calls(store, "call-1")

    with pytest.raises(StateError):
        await store.claim_tool_call(
            "call-1", from_status=ToolCallStatus.COMPLETED, to_status=ToolCallStatus.PENDING
        )


async def test_denied_can_be_written_after_approval_and_never_dispatched(store):
    """审批拒绝：工具从未派发，但必须能落观察结果与工具消息。"""
    session = await _turn_with_calls(store, "call-1")
    await store.set_tool_status("call-1", ToolCallStatus.AWAITING_APPROVAL)

    row = await store.finish_tool_call("call-1", ToolResult("denied", "user refused"))

    assert row.status == ToolCallStatus.DENIED.value
    assert row.reversible is False
    tool_messages = [m for m in await store.list_messages(session.id) if m.role == MessageRole.TOOL]
    assert [m.tool_call_id for m in tool_messages] == ["call-1"]


async def test_dispatched_result_cannot_be_turned_into_denied(store):
    await _turn_with_calls(store, "call-1")
    await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
    )

    with pytest.raises(StateError):
        await store.finish_tool_call("call-1", ToolResult("denied", "too late"))


async def test_execution_requires_a_successful_claim_first(store):
    """approved 不等于 dispatched：没有 claim 成功就不许写结果。"""
    await _turn_with_calls(store, "call-1")
    await store.set_tool_status("call-1", ToolCallStatus.AWAITING_APPROVAL)
    await store.set_tool_status("call-1", ToolCallStatus.APPROVED)

    with pytest.raises(StateError):
        await store.finish_tool_call("call-1", ToolResult("success", "ok"))


# --- 结果、凭据与原子性 ---


async def test_finish_writes_observation_tool_message_and_receipt(store):
    session = await _turn_with_calls(store, "call-1")
    await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
    )

    row = await store.finish_tool_call(
        "call-1", ToolResult("success", {"stdout": "ok"}, receipt="backup-1")
    )

    assert row.status == ToolCallStatus.COMPLETED.value
    assert row.reversible is True
    assert await store.get_receipt("call-1") == "backup-1"

    tool_messages = [m for m in await store.list_messages(session.id) if m.role == MessageRole.TOOL]
    assert len(tool_messages) == 1
    assert '"stdout"' in tool_messages[0].content


async def test_non_reversible_result_has_no_receipt(store):
    await _turn_with_calls(store, "call-1")
    await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
    )
    await store.finish_tool_call("call-1", ToolResult("success", "ok"))

    assert await store.get_receipt("call-1") is None


async def test_failed_result_serialization_keeps_state_and_messages_unchanged(store):
    session = await _turn_with_calls(store, "call-1")
    await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
    )
    before = len(await store.list_messages(session.id))

    with pytest.raises(StateError):
        await store.finish_tool_call("call-1", ToolResult("success", {1, 2, 3}))

    calls = await store.list_tool_calls(session.id)
    assert calls[0].status == ToolCallStatus.DISPATCHED.value
    assert len(await store.list_messages(session.id)) == before


# --- 恢复 ---


async def test_user_message_without_model_reply_is_resumable(store):
    session = await store.create_session()
    await store.append_message(session.id, MessageRole.USER, "看一下这个文件")

    state = await store.load_open_turn(session.id)

    assert state is not None
    assert state.status == TurnStatus.OPEN
    assert state.iteration == 0


async def test_tool_results_waiting_for_next_model_call_are_resumable(store):
    session = await _turn_with_calls(store, "call-1")
    await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
    )
    await store.finish_tool_call("call-1", ToolResult("success", "ok"))

    state = await store.load_open_turn(session.id)

    assert state is not None
    assert state.status == TurnStatus.OPEN
    assert state.iteration == 1


async def test_awaiting_approval_is_reported_as_the_pending_call(store):
    session = await _turn_with_calls(store, "call-1")
    await store.set_tool_status("call-1", ToolCallStatus.AWAITING_APPROVAL)

    state = await store.load_open_turn(session.id)

    assert state is not None
    assert state.status == TurnStatus.WAITING_APPROVAL
    assert state.pending_call_id == "call-1"
    assert state.iteration == 1


async def test_unknown_is_terminal_and_does_not_reopen_on_second_read(store):
    session = await _turn_with_calls(store, "call-1")
    await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
    )

    first = await store.load_open_turn(session.id)
    second = await store.load_open_turn(session.id)

    assert first is not None and first.status == TurnStatus.FAILED
    assert first.notes["unknown_calls"] == ["call-1"]
    assert second is None
    assert (await store.load_session(session.id)).status == SessionStatus.FAILED.value


async def test_unknown_batch_with_surviving_pending_call_does_not_reopen(store):
    """同批一个结果未知、另一个仍 pending：第二次读取不能变回 OPEN。"""
    session = await _turn_with_calls(store, "call-1", "call-2")
    await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
    )

    first = await store.load_open_turn(session.id)
    second = await store.load_open_turn(session.id)

    assert first is not None and first.status == TurnStatus.FAILED
    assert second is None


async def test_abandoned_session_is_never_reopened(store):
    session = await _turn_with_calls(store, "call-1")
    await store.set_tool_status("call-1", ToolCallStatus.AWAITING_APPROVAL)
    await store.update_session(session.id, status=SessionStatus.ABANDONED.value)

    assert await store.load_open_turn(session.id) is None


async def test_summary_appended_after_completion_does_not_reopen(store):
    session = await store.create_session()
    await store.append_message(session.id, MessageRole.USER, "hi")
    await store.record_assistant_turn(session.id, ModelResponse(content="done"))
    await store.append_message(session.id, MessageRole.SUMMARY, "压缩后的历史")

    assert await store.load_open_turn(session.id) is None


async def test_new_user_message_after_completion_opens_a_new_turn(store):
    session = await store.create_session()
    await store.append_message(session.id, MessageRole.USER, "hi")
    await store.record_assistant_turn(session.id, ModelResponse(content="done"))

    await store.append_message(session.id, MessageRole.USER, "再来一轮")

    state = await store.load_open_turn(session.id)
    assert state is not None and state.status == TurnStatus.OPEN


# --- 事务与错误 ---


async def test_transaction_rolls_back_completed_writes(store):
    session = await store.create_session()

    with pytest.raises(RuntimeError):
        async with store.transaction():
            await store.append_message(session.id, MessageRole.USER, "tx")
            raise RuntimeError("boom")

    assert await store.list_messages(session.id) == []


async def test_missing_session_raises_not_found(store):
    with pytest.raises(NotFoundError):
        await store.append_message("nope", MessageRole.USER, "x")
