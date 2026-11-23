"""StateStore 端口契约。

这是「可执行的规格」：任何实现（内存假实现、C 的 SQLite 实现、将来的其它后端）
都必须原样通过。新增实现时只需把它加进 ``store`` 的 params。

C 交付 ``app/state/persistence/sqlite_state_store.py`` 后，把该分支的 skip 换成真实
构造即可，测试本身不需要改动。
"""

import pytest

from app.common.api.llm_provider import ModelToolCall
from app.common.api.state_store import ToolCallStatus, TurnStatus
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


async def _session_with_call(store):
    session = await store.create_session(title="contract")
    assistant = await store.append_message(session.id, "assistant", None)
    await store.record_tool_calls(
        session.id, assistant.id, [ModelToolCall(id="call-1", name="read_file", arguments="{}")]
    )
    return session, assistant


async def test_message_seq_is_per_session_and_starts_at_one(store):
    first = await store.create_session(title="a")
    second = await store.create_session(title="b")

    await store.append_message(first.id, "user", "hello")
    await store.append_message(first.id, "assistant", "hi")
    await store.append_message(second.id, "user", "other")

    assert [m.seq for m in await store.list_messages(first.id)] == [1, 2]
    assert [m.seq for m in await store.list_messages(second.id)] == [1]


async def test_list_messages_after_seq_returns_tail_in_order(store):
    session = await store.create_session()
    for index in range(3):
        await store.append_message(session.id, "user", f"m{index}")

    tail = await store.list_messages(session.id, after_seq=1)
    assert [m.content for m in tail] == ["m1", "m2"]


async def test_tool_calls_are_linked_and_start_pending(store):
    session, assistant = await _session_with_call(store)

    calls = await store.list_tool_calls(session.id)
    assert len(calls) == 1
    assert calls[0].id == "call-1"
    assert calls[0].message_id == assistant.id
    assert calls[0].status == ToolCallStatus.PENDING.value


async def test_finish_tool_call_writes_observation_and_tool_message(store):
    session, _ = await _session_with_call(store)
    await store.set_tool_status("call-1", ToolCallStatus.DISPATCHED)
    await store.finish_tool_call("call-1", ToolResult("success", {"stdout": "ok"}))

    calls = await store.list_tool_calls(session.id)
    assert calls[0].status == ToolCallStatus.COMPLETED.value

    messages = await store.list_messages(session.id)
    tool_messages = [m for m in messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call-1"
    assert '"stdout"' in tool_messages[0].content


async def test_finished_turn_has_no_open_state(store):
    session, _ = await _session_with_call(store)
    await store.set_tool_status("call-1", ToolCallStatus.DISPATCHED)
    await store.finish_tool_call("call-1", ToolResult("success", "done"))
    await store.append_message(session.id, "assistant", "final answer")

    assert await store.load_open_turn(session.id) is None


async def test_awaiting_approval_is_reported_as_the_pending_call(store):
    session, _ = await _session_with_call(store)
    await store.set_tool_status("call-1", ToolCallStatus.AWAITING_APPROVAL)

    state = await store.load_open_turn(session.id)
    assert state is not None
    assert state.status == TurnStatus.WAITING_APPROVAL
    assert state.pending_call_id == "call-1"
    assert state.iteration == 1


async def test_user_message_without_model_reply_is_resumable(store):
    """模型还没回复就中断：这一轮不能凭空消失。"""
    session = await store.create_session()
    await store.append_message(session.id, "user", "看一下这个文件")

    state = await store.load_open_turn(session.id)

    assert state is not None
    assert state.status == TurnStatus.OPEN
    assert state.iteration == 0


async def test_tool_results_waiting_for_next_model_call_are_resumable(store):
    """工具结果已落、还没发起下一次模型调用：同样要能接续。"""
    session, _ = await _session_with_call(store)
    await store.set_tool_status("call-1", ToolCallStatus.DISPATCHED)
    await store.finish_tool_call("call-1", ToolResult("success", "ok"))

    state = await store.load_open_turn(session.id)

    assert state is not None
    assert state.status == TurnStatus.OPEN
    assert state.iteration == 1


async def test_failed_result_serialization_keeps_state_and_messages_unchanged(store):
    """序列化失败不得留下「已完成但没有输出」的半个结果。"""
    session, _ = await _session_with_call(store)
    await store.set_tool_status("call-1", ToolCallStatus.DISPATCHED)
    before = len(await store.list_messages(session.id))

    with pytest.raises(StateError):
        await store.finish_tool_call("call-1", ToolResult("success", {1, 2, 3}))

    calls = await store.list_tool_calls(session.id)
    assert calls[0].status == ToolCallStatus.DISPATCHED.value
    assert len(await store.list_messages(session.id)) == before


async def test_dispatched_without_observation_becomes_unknown_and_is_never_replayed(store):
    session, _ = await _session_with_call(store)
    await store.set_tool_status("call-1", ToolCallStatus.DISPATCHED)

    state = await store.load_open_turn(session.id)

    assert state is not None
    assert state.status == TurnStatus.FAILED
    assert state.notes["unknown_calls"] == ["call-1"]
    calls = await store.list_tool_calls(session.id)
    assert calls[0].status == ToolCallStatus.UNKNOWN.value
    # 标记只发生一次：unknown 是终态，再读就没有待执行的一轮了。
    assert await store.load_open_turn(session.id) is None


async def test_missing_session_raises_not_found(store):
    with pytest.raises(NotFoundError):
        await store.append_message("nope", "user", "x")


async def test_duplicate_tool_call_id_is_rejected(store):
    session, assistant = await _session_with_call(store)
    with pytest.raises(StateError):
        await store.record_tool_calls(
            session.id, assistant.id, [ModelToolCall(id="call-1", name="read_file", arguments="{}")]
        )
