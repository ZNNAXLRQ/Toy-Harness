"""SubagentRunStore 端口契约（C 交付 SQLite 实现后同样要过）。

语义：``save_run`` 一次性创建（重复 id 抛 StateError）、``update_run`` 只更新已有行、
``list_runs`` 按 created_at 升序；崩溃遗留的 ``running`` 由调度器判 ``unknown``，
存储层不自动改判。
"""

import pytest

from app.common.exception import NotFoundError, StateError


async def test_save_and_load_roundtrip(subagent_store):
    row = await subagent_store.save_run(
        "run-1", "session-1", parent_task_id="call-1", context_json='{"prompt": "x"}'
    )

    assert row.id == "run-1"
    assert row.status == "running"

    loaded = await subagent_store.load_run("run-1")
    assert loaded is not None and loaded.context_json == '{"prompt": "x"}'


async def test_duplicate_run_id_is_rejected(subagent_store):
    await subagent_store.save_run("run-1", "session-1")

    with pytest.raises(StateError):
        await subagent_store.save_run("run-1", "session-1")


async def test_update_run_sets_terminal_status_and_result(subagent_store):
    await subagent_store.save_run("run-1", "session-1")

    row = await subagent_store.update_run("run-1", status="success", result_json='{"ok": true}')

    assert row.status == "success"
    assert row.result_json == '{"ok": true}'


async def test_update_unknown_run_raises_not_found(subagent_store):
    with pytest.raises(NotFoundError):
        await subagent_store.update_run("nope", status="failed")


async def test_list_runs_filters_by_session_in_creation_order(subagent_store):
    first = await subagent_store.save_run("run-1", "session-1")
    await subagent_store.save_run("run-2", "session-1")
    await subagent_store.save_run("run-3", "session-2")

    rows = await subagent_store.list_runs("session-1")

    assert [row.id for row in rows] == ["run-1", "run-2"]
    assert rows[0].created_at <= rows[-1].created_at
    assert first.session_id == "session-1"
