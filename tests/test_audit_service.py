"""审计契约：脱敏顺序、detail 序列化、limit 语义（latest N）。"""

import json

import pytest

from app.tools.audit import DefaultAuditService
from tests.fakes.memory_state_store import MemoryStateStore


@pytest.fixture
async def store():
    return MemoryStateStore()


@pytest.fixture
async def service(store):
    return DefaultAuditService(store)


async def test_mapping_detail_is_redacted_before_serialization(service, store, monkeypatch):
    """密钥里带引号/反斜杠时，"先序列化后脱敏"会替换不到——必须按值脱敏再序列化。"""
    monkeypatch.setenv("MY_AUDIT_SECRET", 'ab"cd')
    session = await store.create_session()

    row = await service.log(
        action="tool_denied",
        actor="teacher",
        session_id=session.id,
        detail={"command": 'echo ab"cd', "stdout": "hi"},
    )

    payload = json.loads(row.detail)
    assert payload["command"] == "echo [REDACTED]"
    assert "ab\"cd" not in row.detail
    assert payload["stdout"] == "hi"


async def test_detail_keys_are_preserved(service, store, monkeypatch):
    monkeypatch.setenv("MY_AUDIT_SECRET", "stdout")
    session = await store.create_session()

    row = await service.log(
        action="tool_denied",
        actor="teacher",
        session_id=session.id,
        detail={"stdout": "hello", "returncode": 1},
    )

    payload = json.loads(row.detail)
    assert payload["stdout"] == "hello"
    assert payload["returncode"] == 1


async def test_string_detail_is_redacted(service, store, monkeypatch):
    monkeypatch.setenv("MY_AUDIT_SECRET", "sk-should-not-leak-1234")
    session = await store.create_session()

    row = await service.log(
        action="provider_error", actor="system", session_id=session.id, detail="token sk-should-not-leak-1234"
    )

    assert "sk-should-not-leak-1234" not in (row.detail or "")


async def test_query_returns_latest_n_in_ascending_order(service, store):
    session = await store.create_session()
    for index in range(3):
        await service.log(action=f"a{index}", actor="system", session_id=session.id)

    rows = await service.query(session_id=session.id, limit=2)

    assert [row.action for row in rows] == ["a1", "a2"]


async def test_query_rejects_non_positive_limit(service, store):
    session = await store.create_session()

    with pytest.raises(Exception):
        await service.query(session_id=session.id, limit=0)
