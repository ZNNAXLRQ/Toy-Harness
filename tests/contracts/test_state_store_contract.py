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

import asyncio

import pytest

from app.common.api.llm_provider import ModelResponse, ModelToolCall
from app.common.api.state_store import (
    ApprovalDecision,
    ApprovalStatus,
    MessageRole,
    SessionStatus,
    ToolCallStatus,
    TurnStatus,
)
from app.common.api.tool import ToolResult
from app.common.exception import ApprovalError, ConflictError, NotFoundError, StateError

# ``store`` fixture 在 conftest.py 里：实现方（C）只需要改那一处。


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


async def _request_approval(store, session_id: str, call_id: str):
    """唯一合法的等待态来源：审批行与 awaiting 状态同事务写入。"""
    call = next(c for c in await store.list_tool_calls(session_id) if c.id == call_id)
    return await store.record_approval_request(
        session_id, call_id, call.tool_name, call.arguments, "needs approval"
    )


async def _approved_call(store, session_id: str, call_id: str):
    """把调用推进到 awaiting（已请求审批）并写入 approved 决策。"""
    approval = await _request_approval(store, session_id, call_id)
    await store.decide_approval(approval.id, ApprovalDecision.APPROVED, actor="teacher")
    return approval


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
    await _request_approval(store, session.id, "call-1")

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
    """approved 不等于 dispatched：没有派发 CAS 就不许写结果。"""
    session = await _turn_with_calls(store, "call-1")
    await _approved_call(store, session.id, "call-1")
    assert await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.AWAITING_APPROVAL, to_status=ToolCallStatus.APPROVED
    )

    with pytest.raises(StateError):
        await store.finish_tool_call("call-1", ToolResult("success", "ok"))


async def test_approved_must_be_claimed_before_dispatch(store):
    session = await _turn_with_calls(store, "call-1")
    await _approved_call(store, session.id, "call-1")
    await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.AWAITING_APPROVAL, to_status=ToolCallStatus.APPROVED
    )

    assert await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.APPROVED, to_status=ToolCallStatus.DISPATCHED
    )
    assert not await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.APPROVED, to_status=ToolCallStatus.DISPATCHED
    )


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
    await _request_approval(store, session.id, "call-1")

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
    await _request_approval(store, session.id, "call-1")
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


# --- 全链路自查补的用例 ---


async def test_set_tool_status_cannot_dispatch(store):
    """派发必须走 claim；set_tool_status 直接写 dispatched 会绕过单飞保证。"""
    await _turn_with_calls(store, "call-1")

    with pytest.raises(StateError):
        await store.set_tool_status("call-1", ToolCallStatus.DISPATCHED)


async def test_late_result_after_abandoned_session_is_rejected(store):
    """已经终止的一轮不能被迟到的工具结果「救活」。"""
    session = await _turn_with_calls(store, "call-1")
    await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
    )
    await store.update_session(session.id, status=SessionStatus.ABANDONED.value)

    with pytest.raises(ConflictError):
        await store.finish_tool_call("call-1", ToolResult("success", "late"))


async def test_update_session_rejects_id_change(store):
    session = await store.create_session()

    with pytest.raises(StateError):
        await store.update_session(session.id, id="forged")


async def test_update_session_rejects_unknown_status(store):
    session = await store.create_session()

    with pytest.raises(StateError):
        await store.update_session(session.id, status="nonsense")


async def test_two_awaiting_approvals_in_one_turn_are_rejected(store):
    """同一时刻只允许一条待审批；出现两条说明上一轮挂起没被消费。"""
    session = await _turn_with_calls(store, "call-1", "call-2")
    await _request_approval(store, session.id, "call-1")
    await _request_approval(store, session.id, "call-2")

    with pytest.raises(StateError):
        await store.load_open_turn(session.id)


async def test_denial_terminates_the_turn(store):
    """拒绝即终止：尾部是工具消息也不能把这一轮重新打开。"""
    session = await _turn_with_calls(store, "call-1")
    await _request_approval(store, session.id, "call-1")
    await store.finish_tool_call("call-1", ToolResult("denied", "refused"))

    assert await store.load_open_turn(session.id) is None

    # 用户重新发消息属于新一轮。
    await store.append_message(session.id, MessageRole.USER, "换个方式")
    state = await store.load_open_turn(session.id)
    assert state is not None and state.status == TurnStatus.OPEN


async def test_iteration_counts_every_model_call_in_the_turn(store):
    session = await _turn_with_calls(store, "call-1")
    await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
    )
    await store.finish_tool_call("call-1", ToolResult("success", "ok"))
    await store.record_assistant_turn(session.id, _response("call-2"))

    state = await store.load_open_turn(session.id)

    assert state is not None and state.iteration == 2
# --- 审批记录（approvals 表）---


async def _request_approval(store, session_id: str, call_id: str):
    call = next(c for c in await store.list_tool_calls(session_id) if c.id == call_id)
    return await store.record_approval_request(
        session_id, call_id, call.tool_name, call.arguments, "needs approval"
    )


async def test_approval_request_and_decision_roundtrip(store):
    session = await _turn_with_calls(store, "call-1")

    approval = await _request_approval(store, session.id, "call-1")
    assert approval.status == ApprovalStatus.PENDING.value
    calls = await store.list_tool_calls(session.id)
    assert calls[0].status == ToolCallStatus.AWAITING_APPROVAL.value
    assert [a.id for a in await store.list_pending_approvals(session.id)] == [approval.id]

    decided = await store.decide_approval(approval.id, ApprovalDecision.APPROVED, actor="teacher")
    assert decided.status == ApprovalStatus.APPROVED.value
    assert decided.decided_by == "teacher"

    # 决策后不在 pending 队列里，但仍可按 call 查回（恢复用）。
    assert await store.list_pending_approvals(session.id) == []
    assert (await store.get_approval_for_call("call-1")).id == approval.id

    # 决策不改调用状态：消费是独立的一次 CAS。
    calls = await store.list_tool_calls(session.id)
    assert calls[0].status == ToolCallStatus.AWAITING_APPROVAL.value


async def test_approval_request_is_idempotent_and_decision_is_single_shot(store):
    session = await _turn_with_calls(store, "call-1")

    first = await _request_approval(store, session.id, "call-1")
    second = await _request_approval(store, session.id, "call-1")
    assert first.id == second.id

    await store.decide_approval(first.id, ApprovalDecision.DENIED, actor="teacher")
    with pytest.raises(ApprovalError):
        await store.decide_approval(first.id, ApprovalDecision.APPROVED, actor="teacher")


async def test_decided_approval_is_recoverable_after_restart(store):
    """已决策未消费：重启后 load_open_turn 必须同时给出 call 与 approval id。"""
    session = await _turn_with_calls(store, "call-1")
    approval = await _request_approval(store, session.id, "call-1")
    await store.decide_approval(approval.id, ApprovalDecision.APPROVED, actor="teacher")

    state = await store.load_open_turn(session.id)

    assert state is not None
    assert state.status == TurnStatus.WAITING_APPROVAL
    assert state.pending_call_id == "call-1"
    assert state.pending_approval_id == approval.id


async def test_undecided_approval_reports_only_the_call(store):
    session = await _turn_with_calls(store, "call-1")
    await _request_approval(store, session.id, "call-1")

    state = await store.load_open_turn(session.id)

    assert state is not None and state.status == TurnStatus.WAITING_APPROVAL
    assert state.pending_call_id == "call-1"
    assert state.pending_approval_id is None


# --- 恢复：pending / approved / summary / 只读投影 ---


async def test_pending_call_after_assistant_message_is_resumable(store):
    """模型已回、工具还没派发就崩溃：这一轮不能丢。"""
    session = await _turn_with_calls(store, "call-1")

    state = await store.load_open_turn(session.id)

    assert state is not None
    assert state.status == TurnStatus.OPEN
    assert state.iteration == 1


async def test_approved_call_is_resumable_and_still_open(store):
    """崩在两次 CAS 之间（approved）：仍能恢复，等派发。"""
    session = await _turn_with_calls(store, "call-1")
    await _approved_call(store, session.id, "call-1")
    await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.AWAITING_APPROVAL, to_status=ToolCallStatus.APPROVED
    )

    state = await store.load_open_turn(session.id)

    assert state is not None and state.status == TurnStatus.OPEN
    assert state.open_call_ids == ("call-1",)


async def test_summary_in_the_middle_does_not_close_the_turn(store):
    """工具结果之后追加摘要，不能把还有待执行调用的一轮判成结束。"""
    session = await _turn_with_calls(store, "call-1")
    await store.append_message(session.id, MessageRole.SUMMARY, "压缩后的历史")

    state = await store.load_open_turn(session.id)

    assert state is not None and state.status == TurnStatus.OPEN


async def test_peek_does_not_kill_an_in_flight_call(store):
    """只读投影：正在执行的调用不能被判 UNKNOWN。"""
    session = await _turn_with_calls(store, "call-1")
    await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
    )

    state = await store.peek_open_turn(session.id)

    assert state is not None
    assert state.notes["in_flight"] == ["call-1"]
    calls = await store.list_tool_calls(session.id)
    assert calls[0].status == ToolCallStatus.DISPATCHED.value


# --- 终态与并发约束 ---


async def test_claim_on_terminal_session_is_rejected(store):
    session = await _turn_with_calls(store, "call-1")
    await store.update_session(session.id, status=SessionStatus.FAILED.value)

    with pytest.raises(ConflictError):
        await store.claim_tool_call(
            "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
        )


async def test_terminal_session_cannot_be_updated(store):
    session = await _turn_with_calls(store, "call-1")
    await store.update_session(session.id, status=SessionStatus.FAILED.value)

    with pytest.raises(StateError):
        await store.update_session(session.id, status=SessionStatus.OPEN.value)


async def test_claim_only_allows_consume_and_dispatch_targets(store):
    await _turn_with_calls(store, "call-1")

    with pytest.raises(StateError):
        await store.claim_tool_call(
            "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DENIED
        )


async def test_concurrent_claim_has_exactly_one_winner(store):
    await _turn_with_calls(store, "call-1")

    results = await asyncio.gather(
        store.claim_tool_call(
            "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
        ),
        store.claim_tool_call(
            "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
        ),
    )

    assert sorted(results) == [False, True]


async def test_duplicate_call_ids_leave_no_message(store):
    session = await store.create_session()

    with pytest.raises(StateError):
        await store.record_assistant_turn(session.id, _response("same", "same"))

    assert list(await store.list_messages(session.id)) == []
    assert list(await store.list_tool_calls(session.id)) == []


async def test_terminal_call_cannot_be_moved_directly(store):
    session = await _turn_with_calls(store, "call-1")
    await store.claim_tool_call(
        "call-1", from_status=ToolCallStatus.PENDING, to_status=ToolCallStatus.DISPATCHED
    )
    await store.finish_tool_call("call-1", ToolResult("success", "ok"))

    with pytest.raises(StateError):
        await store.set_tool_status("call-1", ToolCallStatus.PENDING)


async def test_transaction_rolls_back_tool_status_and_approvals(store):
    session = await _turn_with_calls(store, "call-1")

    with pytest.raises(RuntimeError):
        async with store.transaction():
            await _request_approval(store, session.id, "call-1")
            raise RuntimeError("boom")

    calls = await store.list_tool_calls(session.id)
    assert calls[0].status == ToolCallStatus.PENDING.value
    assert await store.list_pending_approvals(session.id) == []

# --- 审批消费的安全约束 ---


async def test_set_tool_status_cannot_create_awaiting_state(store):
    """等待态只能由 record_approval_request 产生，否则会出现无审批记录的卡死轮次。"""
    await _turn_with_calls(store, "call-1")

    with pytest.raises(StateError):
        await store.set_tool_status("call-1", ToolCallStatus.AWAITING_APPROVAL)


async def test_claim_to_approved_requires_an_approved_decision(store):
    """没有已批准的审批记录，不许进入 approved——否则未批准的工具会被执行。"""
    session = await _turn_with_calls(store, "call-1")
    await _request_approval(store, session.id, "call-1")  # 决策仍未提交

    with pytest.raises(ConflictError):
        await store.claim_tool_call(
            "call-1",
            from_status=ToolCallStatus.AWAITING_APPROVAL,
            to_status=ToolCallStatus.APPROVED,
        )


async def test_denied_decision_cannot_be_consumed_as_approved(store):
    session = await _turn_with_calls(store, "call-1")
    approval = await _request_approval(store, session.id, "call-1")
    await store.decide_approval(approval.id, ApprovalDecision.DENIED, actor="teacher")

    with pytest.raises(ConflictError):
        await store.claim_tool_call(
            "call-1",
            from_status=ToolCallStatus.AWAITING_APPROVAL,
            to_status=ToolCallStatus.APPROVED,
        )


# --- 审计 ---


async def test_decide_approval_writes_an_audit_row(store):
    session = await _turn_with_calls(store, "call-1")
    approval = await _request_approval(store, session.id, "call-1")

    await store.decide_approval(approval.id, ApprovalDecision.APPROVED, actor="teacher")

    rows = list(await store.query_audits(session_id=session.id))
    decided = [row for row in rows if row.action == "approval_decided"]
    assert len(decided) == 1
    assert decided[0].actor == "teacher"
    assert approval.id in (decided[0].detail or "")
    assert "approved" in (decided[0].detail or "")


async def test_audit_records_are_append_only_and_queryable(store):
    session = await store.create_session()
    await store.record_audit(action="session_created", actor="system", session_id=session.id)
    await store.record_audit(
        action="rollback_performed", actor="teacher", session_id=session.id, detail='{"n": 1}'
    )

    rows = list(await store.query_audits(session_id=session.id))

    assert [row.action for row in rows] == ["session_created", "rollback_performed"]
    assert rows[-1].detail == '{"n": 1}'


async def test_latest_message_seq_tracks_appended_messages(store):
    session = await store.create_session()
    assert await store.latest_message_seq(session.id) == 0

    await store.append_message(session.id, MessageRole.USER, "a")
    await store.append_message(session.id, MessageRole.ASSISTANT, "b")

    assert await store.latest_message_seq(session.id) == 2


# --- 终态会话的写入守卫 ---


async def test_terminal_session_rejects_message_writes(store):
    session = await _turn_with_calls(store, "call-1")
    await store.update_session(session.id, status=SessionStatus.FAILED.value)

    with pytest.raises(ConflictError):
        await store.append_message(session.id, MessageRole.USER, "late")
    with pytest.raises(ConflictError):
        await store.record_assistant_turn(session.id, ModelResponse(content="late"))


async def test_terminal_session_rejects_approval_writes(store):
    session = await _turn_with_calls(store, "call-1")
    await _request_approval(store, session.id, "call-1")
    await store.update_session(session.id, status=SessionStatus.ABANDONED.value)

    with pytest.raises(ConflictError):
        await store.record_approval_request(session.id, "call-1", "write_file", "{}", "d")


async def test_pending_call_reports_open_call_ids(store):
    session = await _turn_with_calls(store, "call-1", "call-2")

    state = await store.load_open_turn(session.id)

    assert state is not None and state.status == TurnStatus.OPEN
    assert state.open_call_ids == ("call-1", "call-2")

# --- 审计 limit 语义与审批过期 ---


async def test_query_audits_returns_latest_n_in_ascending_order(store):
    session = await store.create_session()
    for index in range(3):
        await store.record_audit(action=f"a{index}", actor="system", session_id=session.id)

    rows = await store.query_audits(session_id=session.id, limit=2)

    assert [row.action for row in rows] == ["a1", "a2"]


async def test_query_audits_rejects_non_positive_limit(store):
    with pytest.raises(StateError):
        await store.query_audits(limit=0)


async def test_abandoned_session_pending_approvals_are_expired_and_hidden(store):
    """放弃一轮后，待审批要过期并写审计，不能留在重连快照里变成幽灵项。"""
    session = await _turn_with_calls(store, "call-1")
    approval = await _request_approval(store, session.id, "call-1")
    await store.update_session(session.id, status=SessionStatus.ABANDONED.value)

    # 终态会话的遗留审批不再出现在待审批队列里
    assert await store.list_pending_approvals(session.id) == []

    expired = await store.expire_pending_approvals(session.id, actor="system:abandon")
    assert [row.id for row in expired] == [approval.id]

    rows = list(await store.query_audits(session_id=session.id))
    assert any(row.action == "approval_expired" for row in rows)

    with pytest.raises(ApprovalError):
        await store.decide_approval(approval.id, ApprovalDecision.APPROVED, actor="teacher")

async def test_pre_dispatch_validation_error_can_finish_as_failed(store):
    """执行前校验失败（坏 JSON / 未知工具）没有外部副作用，允许 pending → failed。"""
    session = await _turn_with_calls(store, "call-1")

    row = await store.finish_tool_call("call-1", ToolResult("error", "bad arguments"))

    assert row.status == ToolCallStatus.FAILED.value
    tool_messages = [m for m in await store.list_messages(session.id) if m.role == MessageRole.TOOL]
    assert len(tool_messages) == 1

async def test_pending_call_cannot_finish_as_success(store):
    """执行前失败只允许 pending→failed；success 必须先 dispatched。"""
    await _turn_with_calls(store, "call-1")

    with pytest.raises(StateError):
        await store.finish_tool_call("call-1", ToolResult("success", "should not pass"))
