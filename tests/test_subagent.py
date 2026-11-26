"""子代理调度测试：白名单/深度/并发/预算/取消/恢复/事件。

全部离线：provider、工具、运行记录、事件都是替身。
"""

import asyncio
import json

import pytest

from app.common.api.llm_provider import ModelResponse, ModelToolCall
from app.common.api.subagent_scheduler import (
    SubagentResult,
    SubagentRunStatus,
    SubagentTask,
)
from app.common.exception import SubagentError
from app.core.subagent import (
    DefaultSubagentScheduler,
    DelegationPolicy,
    merge_subagent_result,
    result_json,
)
from tests.fakes.fake_context_events import RecordingEventPublisher
from tests.fakes.fake_subagent_store import MemorySubagentRunStore
from tests.fakes.memory_state_store import MemoryStateStore
from tests.fakes.fake_tools import FakeToolRegistry


def response(*calls, content=None, finish_reason=None, tokens=0) -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=list(calls),
        finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
        tokens=tokens,
    )


def call(call_id: str, name: str, arguments: dict | str) -> ModelToolCall:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return ModelToolCall(id=call_id, name=name, arguments=raw)


def task(**overrides) -> SubagentTask:
    defaults = dict(
        id="t1",
        parent_session_id="s1",
        parent_call_id="c1",
        prompt="read notes.txt",
    )
    defaults.update(overrides)
    return SubagentTask(**defaults)


class Harness:
    def __init__(self, responses, *, policy: DelegationPolicy | None = None):
        self.provider = ScriptedProvider(list(responses))
        self.tools = FakeToolRegistry()
        self.tools.add("read_file", lambda arguments: "content of x")
        self.tools.add("list_files", lambda arguments: ["x"])
        self.tools.add("write_file", lambda arguments: {"written": True})
        self.store = MemorySubagentRunStore()
        self.events = RecordingEventPublisher()
        self.policy = policy or DelegationPolicy(
            allowed_tools=frozenset({"read_file", "list_files"}),
            max_concurrency=1,
            max_depth=2,
            max_iterations=3,
            token_budget=100,
        )
        self.scheduler = DefaultSubagentScheduler(
            provider=self.provider,
            tools=self.tools,
            run_store=self.store,
            events=self.events,
            policy=self.policy,
        )

    async def spawn(self, **overrides) -> SubagentResult:
        return await self.scheduler.spawn(task(**overrides))


class ScriptedProvider:
    provider_name = "scripted"

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if not self._responses:
            raise RuntimeError("ScriptedProvider has no more scripted responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# --- 正常路径 ---


async def test_spawn_runs_read_only_tool_and_records_result():
    harness = Harness(
        [
            response(call("r1", "read_file", {"file_path": "x"}), tokens=10),
            response(content="notes say hi", tokens=5),
        ]
    )

    result = await harness.spawn()

    assert result.status == SubagentRunStatus.SUCCESS.value
    assert result.summary == "notes say hi"
    assert result.tokens == 15
    assert result.steps == 2
    assert harness.tools.executed == [("read_file", {"file_path": "x"})]

    stored = await harness.store.load_run(result.run_id)
    assert stored is not None and stored.status == "success"
    assert json.loads(stored.result_json)["summary"] == "notes say hi"
    assert harness.events.kinds() == ["subagent_started", "subagent_finished"]


async def test_requested_tools_must_be_within_policy():
    harness = Harness([])

    with pytest.raises(SubagentError):
        await harness.spawn(allowed_tools=("write_file",))

    assert await harness.store.list_runs("s1") == []


async def test_hallucinated_tool_is_denied_and_fails_the_run():
    harness = Harness([response(call("w1", "write_file", {"file_path": "x"}))])

    result = await harness.spawn()

    assert result.status == SubagentRunStatus.FAILED.value
    assert result.summary == "subagent tool denied"
    assert harness.tools.executed == []


# --- 策略边界 ---


async def test_depth_beyond_policy_is_rejected():
    harness = Harness([])

    with pytest.raises(SubagentError):
        await harness.spawn(depth=3)


async def test_max_iterations_is_bounded_by_policy():
    harness = Harness(
        [
            response(call("r1", "read_file", {"file_path": "x"})),
            response(call("r2", "read_file", {"file_path": "x"})),
            response(call("r3", "read_file", {"file_path": "x"})),
        ]
    )

    result = await harness.spawn()

    assert result.status == SubagentRunStatus.FAILED.value
    assert "maximum iterations" in result.summary
    assert result.steps == 3


async def test_token_budget_is_enforced():
    harness = Harness([response(content="done", tokens=500)])

    result = await harness.spawn()

    assert result.status == SubagentRunStatus.FAILED.value
    assert "token budget" in result.summary


async def test_provider_error_marks_the_run_failed():
    harness = Harness([RuntimeError("upstream down")])

    result = await harness.spawn()

    assert result.status == SubagentRunStatus.FAILED.value
    assert "RuntimeError" in result.summary
    stored = await harness.store.load_run(result.run_id)
    assert stored is not None and stored.status == "failed"


# --- 并发 / 取消 / 恢复 ---


async def test_concurrency_is_capped_by_policy():
    class SlowProvider:
        provider_name = "slow"

        def __init__(self):
            self.active = 0
            self.max_active = 0

        async def complete(self, request):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return response(content="done")

    provider = SlowProvider()
    scheduler = DefaultSubagentScheduler(
        provider=provider,
        tools=FakeToolRegistry(),
        run_store=MemorySubagentRunStore(),
        events=RecordingEventPublisher(),
        policy=DelegationPolicy(
            allowed_tools=frozenset({"read_file"}),
            max_concurrency=1,
            max_depth=2,
            max_iterations=2,
            token_budget=100,
        ),
    )
    registry = scheduler.tools  # FakeToolRegistry for schemas
    registry.add("read_file", lambda arguments: "x")

    results = await asyncio.gather(
        *(scheduler.spawn(task(id=f"t{i}")) for i in range(3))
    )

    assert [result.status for result in results] == ["success", "success", "success"]
    assert provider.max_active == 1


async def test_cancel_makes_spawn_return_cancelled_result():
    started = asyncio.Event()

    class BlockingProvider:
        provider_name = "blocking"

        async def complete(self, request):
            started.set()
            await asyncio.Event().wait()  # 永远等下去，直到被 cancel

    store = MemorySubagentRunStore()
    scheduler = DefaultSubagentScheduler(
        provider=BlockingProvider(),
        tools=FakeToolRegistry(),
        run_store=store,
        events=RecordingEventPublisher(),
        policy=DelegationPolicy(allowed_tools=frozenset({"read_file"}), max_concurrency=1),
    )
    scheduler.tools.add("read_file", lambda arguments: "x")

    spawn_task = asyncio.create_task(scheduler.spawn(task()))
    await started.wait()
    runs = await store.list_runs("s1")
    assert len(runs) == 1 and runs[0].status == "running"

    await scheduler.cancel(runs[0].id)
    done, pending = await asyncio.wait({spawn_task}, timeout=2)

    assert pending == set(), "cancel 后 spawn 必须在超时前结束"
    assert done == {spawn_task}
    result = spawn_task.result()
    assert result.status == SubagentRunStatus.CANCELLED.value
    stored = await store.load_run(runs[0].id)
    assert stored is not None and stored.status == "cancelled"


async def test_recover_unknown_marks_stale_running_rows():
    store = MemorySubagentRunStore()
    await store.save_run("run-1", "s1")
    await store.save_run("run-2", "s1")
    await store.update_run("run-2", status="success")
    scheduler = DefaultSubagentScheduler(
        provider=ScriptedProvider([]),
        tools=FakeToolRegistry(),
        run_store=store,
        events=RecordingEventPublisher(),
    )

    recovered = await scheduler.recover_unknown("s1")

    assert recovered == 1
    assert (await store.load_run("run-1")).status == "unknown"
    assert (await store.load_run("run-2")).status == "success"
    assert await scheduler.recover_unknown("s1") == 0


async def test_running_lists_only_active_or_unknown_runs():
    store = MemorySubagentRunStore()
    await store.save_run("run-1", "s1")
    await store.save_run("run-2", "s1")
    await store.update_run("run-2", status="success")
    scheduler = DefaultSubagentScheduler(
        provider=ScriptedProvider([]),
        tools=FakeToolRegistry(),
        run_store=store,
        events=RecordingEventPublisher(),
    )

    rows = await scheduler.running("s1")

    assert [row.id for row in rows] == ["run-1"]


# --- 结果回流文本 ---


def test_result_merger_produces_parseable_bounded_observation():
    result = SubagentResult(
        run_id="r1",
        status="success",
        summary="x" * 9000,
        tokens=12,
        steps=3,
    )

    text = merge_subagent_result(result, max_chars=500)
    payload = json.loads(text)

    assert len(text) <= 500
    assert payload["subagent_status"] == "success"
    assert payload["summary"].endswith("[truncated]")
    assert payload["tokens"] == 12


def test_result_json_keeps_detail_for_audit():
    result = SubagentResult(
        run_id="r1", status="failed", summary="boom", detail={"reason": "x"}
    )

    payload = json.loads(result_json(result))

    assert payload["status"] == "failed"
    assert payload["detail"] == {"reason": "x"}

# --- PR3 复审修复（P1/P2）补的用例 ---


def test_policy_refuses_non_read_only_tools():
    """子代理只读是硬边界：不能通过配置把 write_file 打开。"""
    with pytest.raises(SubagentError):
        DelegationPolicy(allowed_tools=frozenset({"read_file", "write_file"}))
    with pytest.raises(SubagentError):
        DelegationPolicy(allowed_tools=frozenset({"execute_command"}))


async def test_cancel_terminal_run_is_a_noop():
    harness = Harness([response(content="done")])
    result = await harness.spawn()

    await harness.scheduler.cancel(result.run_id)  # 端口约定：终态空操作

    stored = await harness.store.load_run(result.run_id)
    assert stored is not None and stored.status == "success"


async def test_cancel_while_started_event_is_publishing_still_updates_store():
    class BlockingStartedPublisher:
        def __init__(self):
            self.entered = asyncio.Event()
            self.events = []

        async def publish(self, event):
            self.events.append(event)
            if str(event.type) == "subagent_started":
                self.entered.set()
                await asyncio.Event().wait()  # 模拟慢 SSE 消费者，直到被取消

    publisher = BlockingStartedPublisher()
    store = MemorySubagentRunStore()
    scheduler = DefaultSubagentScheduler(
        provider=ScriptedProvider([response(content="done")]),
        tools=FakeToolRegistry(),
        run_store=store,
        events=publisher,
    )
    scheduler.tools.add("read_file", lambda arguments: "x")

    spawn_task = asyncio.create_task(scheduler.spawn(task()))
    await publisher.entered.wait()
    runs = await store.list_runs("s1")
    assert len(runs) == 1 and runs[0].status == "running"

    await scheduler.cancel(runs[0].id)
    done, pending = await asyncio.wait({spawn_task}, timeout=2)

    assert pending == set() and done == {spawn_task}
    assert spawn_task.result().status == SubagentRunStatus.CANCELLED.value
    stored = await store.load_run(runs[0].id)
    assert stored is not None and stored.status == "cancelled"


async def test_recover_unknown_skips_active_runs():
    started = asyncio.Event()

    class BlockingProvider:
        provider_name = "blocking"

        async def complete(self, request):
            started.set()
            await asyncio.Event().wait()

    store = MemorySubagentRunStore()
    scheduler = DefaultSubagentScheduler(
        provider=BlockingProvider(),
        tools=FakeToolRegistry(),
        run_store=store,
        events=RecordingEventPublisher(),
    )
    scheduler.tools.add("read_file", lambda arguments: "x")

    spawn_task = asyncio.create_task(scheduler.spawn(task()))
    await started.wait()
    run_id = (await store.list_runs("s1"))[0].id

    assert await scheduler.recover_unknown("s1") == 0  # 活跃 run 不能被误判
    await scheduler.cancel(run_id)
    await asyncio.wait({spawn_task}, timeout=2)


async def test_running_includes_unknown_and_excludes_terminal():
    store = MemorySubagentRunStore()
    await store.save_run("run-1", "s1")
    await store.save_run("run-2", "s1")
    await store.save_run("run-3", "s1")
    await store.update_run("run-2", status="unknown")
    await store.update_run("run-3", status="success")
    scheduler = DefaultSubagentScheduler(
        provider=ScriptedProvider([]),
        tools=FakeToolRegistry(),
        run_store=store,
        events=RecordingEventPublisher(),
    )

    rows = await scheduler.running("s1")

    assert [row.id for row in rows] == ["run-1", "run-2"]


async def test_subagent_events_have_no_message_seq_and_valid_payload():
    harness = Harness([response(content="done")])
    result = await harness.spawn()

    started, finished = harness.events.events
    assert started.seq == 0 and finished.seq == 0  # 子代理不落会话消息，没有消息锚点
    assert set(started.payload) == {"run_id", "task_id", "prompt"}
    assert set(finished.payload) == {"run_id", "status", "summary", "tokens"}
    assert finished.payload["run_id"] == result.run_id


async def test_parent_session_is_not_modified_by_subagent():
    parent = MemoryStateStore()
    session = await parent.create_session()
    await parent.append_message(session.id, "user", "parent turn")

    tools = FakeToolRegistry()
    tools.add("read_file", lambda arguments: "x")
    scheduler = DefaultSubagentScheduler(
        provider=ScriptedProvider([response(content="done")]),
        tools=tools,
        run_store=MemorySubagentRunStore(),
        events=RecordingEventPublisher(),
    )
    before = await parent.list_messages(session.id)

    await scheduler.spawn(task(parent_session_id=session.id))

    after = await parent.list_messages(session.id)
    assert [(m.id, m.role, m.content) for m in before] == [
        (m.id, m.role, m.content) for m in after
    ]
    assert await parent.list_tool_calls(session.id) == []


async def test_incomplete_subagent_response_fails_without_executing_tools():
    harness = Harness(
        [response(call("r1", "read_file", {"file_path": "x"}), finish_reason="length")]
    )

    result = await harness.spawn()

    assert result.status == SubagentRunStatus.FAILED.value
    assert result.summary == "subagent response incomplete"
    assert harness.tools.executed == []


def test_merger_bounds_escape_heavy_summary():
    result = SubagentResult(run_id="r1", status="success", summary='"\\' * 4000, tokens=1, steps=2)

    text = merge_subagent_result(result, max_chars=500)

    assert len(text) <= 500
    assert json.loads(text)["subagent_status"] == "success"


def test_merger_rejects_too_small_budget():
    result = SubagentResult(run_id="r1", status="success", summary="x" * 100)

    with pytest.raises(ValueError):
        merge_subagent_result(result, max_chars=100)

async def test_default_policy_uses_only_the_tools_that_are_registered():
    """任务没显式请求工具时，策略∩可用即可，不因为注册表缺 list_files 而失败。"""
    tools = FakeToolRegistry()
    tools.add("read_file", lambda arguments: "x")
    scheduler = DefaultSubagentScheduler(
        provider=ScriptedProvider([response(content="done")]),
        tools=tools,
        run_store=MemorySubagentRunStore(),
        events=RecordingEventPublisher(),
    )

    result = await scheduler.spawn(task())

    assert result.status == SubagentRunStatus.SUCCESS.value
