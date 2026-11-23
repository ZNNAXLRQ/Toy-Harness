"""事件契约：payload 白名单、必填键、构造即脱敏、时间单位、SSE 形状。"""

import json
import time

import pytest

from app.common.event import AgentEvent, EventType
from app.common.event.event_type import PAYLOAD_FIELDS, REQUIRED_PAYLOAD_FIELDS


def test_event_serializes_to_sse_frame():
    event = AgentEvent(
        type=EventType.TOOL_END,
        session_id="s1",
        seq=3,
        payload={"call_id": "c1", "status": "success", "result": {"stdout": "ok"}},
    )

    frame = event.to_sse()

    assert frame["event"] == "tool_end"
    body = json.loads(frame["data"])
    assert body["type"] == "tool_end"
    assert body["session_id"] == "s1"
    assert body["seq"] == 3


def test_event_payload_is_validated_against_the_contract():
    with pytest.raises(ValueError):
        AgentEvent(type=EventType.TOOL_START, session_id="s1", payload={"call_id": "c1", "name": "x", "secret": "boom"})

    with pytest.raises(ValueError):
        AgentEvent(type=EventType.TOOL_START, session_id="s1", payload={"call_id": "c1"})


def test_every_event_type_has_a_declared_payload_contract():
    assert set(PAYLOAD_FIELDS) == set(EventType)
    assert set(REQUIRED_PAYLOAD_FIELDS) == set(EventType)
    for event_type, required in REQUIRED_PAYLOAD_FIELDS.items():
        assert required <= PAYLOAD_FIELDS[event_type], event_type


def test_payload_is_redacted_on_construction(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-verysecretvalue123456")
    event = AgentEvent(
        type=EventType.TOOL_END,
        session_id="s1",
        payload={"call_id": "c1", "status": "error", "result": "Authorization: Bearer sk-verysecretvalue123456"},
    )

    assert "sk-verysecretvalue123456" not in json.dumps(event.payload)
    assert "[REDACTED]" in json.dumps(event.payload)


def test_created_at_is_milliseconds():
    event = AgentEvent(type=EventType.TURN_COMPLETED, session_id="s1", payload={"iterations": 1})

    assert isinstance(event.created_at, int)
    assert abs(event.created_at - int(time.time() * 1000)) < 5_000
