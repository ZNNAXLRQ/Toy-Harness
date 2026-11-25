"""HTTP/SSE acceptance tests using only the offline demo provider."""

import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.web.server import create_app
from app.web import server as web_server


@pytest.fixture
def live_server(monkeypatch, request):
    """Run the real ASGI app on loopback so an approval can arrive mid-stream."""
    monkeypatch.setenv("TOY_HARNESS_PROVIDER", "demo")
    app = create_app(approval_timeout=getattr(request, "param", 60.0))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    sock.setblocking(False)
    host, port = sock.getsockname()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    base_url = f"http://{host}:{port}"
    deadline = time.monotonic() + 5
    while True:
        try:
            with httpx.Client(base_url=base_url, timeout=0.25) as client:
                response = client.get("/api/config")
            break
        except (httpx.ConnectError, httpx.ReadTimeout):
            if time.monotonic() >= deadline:
                raise RuntimeError("web test server did not start")
    yield base_url
    server.should_exit = True
    thread.join(timeout=5)
    sock.close()


def _start(client, mode):
    config = client.get("/api/config")
    assert config.status_code == 200
    body = config.json()
    assert body["provider"] == "demo"
    assert isinstance(body["demo_prompt"], str) and body["demo_prompt"]
    response = client.post("/api/runs", json={"prompt": body["demo_prompt"], "mode": mode})
    assert response.status_code == 202
    return response.json()["run_id"]


def _events(client, run_id, *, last_event_id=None):
    headers = {"Accept": "text/event-stream"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)
    with client.stream("GET", f"/api/runs/{run_id}/events", headers=headers, timeout=10) as response:
        assert response.status_code == 200
        event_name, event_id, data = None, None, []
        for line in response.iter_lines():
            if line == "":
                if data:
                    event = json.loads("\n".join(data))
                    event["_sse_event"] = event_name
                    event["_sse_id"] = event_id
                    yield event
                event_name, event_id, data = None, None, []
            elif line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("id:"):
                event_id = line[3:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())


def _terminal_events(client, run_id, *, last_event_id=None):
    events = []
    for event in _events(client, run_id, last_event_id=last_event_id):
        events.append(event)
    assert events and events[-1]["type"] in {"turn_completed", "turn_failed"}
    return events


def test_reconnect_rejects_expired_cursor(live_server, monkeypatch):
    monkeypatch.setattr(web_server, "MAX_EVENTS", 6)
    with httpx.Client(base_url=live_server) as client:
        run_id = _start(client, "full")
        events = _terminal_events(client, run_id)
        assert events[-1]["seq"] > 7
        result = client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "1"})
        assert result.status_code == 410


def _assert_event_contract(events, run_id):
    previous_seq = 0
    for event in events:
        assert {"type", "session_id", "seq", "payload", "created_at"} <= event.keys()
        assert event["session_id"] == run_id
        assert isinstance(event["seq"], int) and event["seq"] > previous_seq
        assert event["_sse_id"] == str(event["seq"])
        assert event["_sse_event"] == event["type"]
        assert isinstance(event["created_at"], int)
        previous_seq = event["seq"]


def test_demo_full_run_reads_then_writes_without_shell(live_server):
    with httpx.Client(base_url=live_server) as client:
        run_id = _start(client, "full")
        events = _terminal_events(client, run_id)

        _assert_event_contract(events, run_id)
        types = [event["type"] for event in events]
        assert "model_request" in types
        model_requests = [event["payload"] for event in events if event["type"] == "model_request"]
        assert model_requests and all(request["provider"] == "demo" for request in model_requests)
        assert types.index("tool_end") < len(types) - 1
        tools = [event["payload"].get("name") for event in events if event["type"] == "tool_start"]
        assert tools == ["read_file", "write_file"]
        assert "shell" not in tools
        reads = [event["payload"] for event in events if event["type"] == "tool_end"
                 and event["payload"].get("name") == "read_file"]
        assert len(reads) == 1
        assert "待讨论" in reads[0]["result"]
        writes = [event["payload"] for event in events if event["type"] == "tool_end"
                  and event["payload"].get("name") == "write_file"]
        assert len(writes) == 1
        write_result = writes[0]["result"]
        assert write_result["file_path"] == "notes.txt"
        assert write_result["characters_written"] > 0
        assert events[-1]["type"] == "turn_completed"


def test_approval_run_pauses_before_write_and_can_be_approved(live_server):
    with httpx.Client(base_url=live_server) as client:
        run_id = _start(client, "approval")
        events = []
        stream = _events(client, run_id)
        for event in stream:
            events.append(event)
            if event["type"] == "approval_required":
                break
        assert events and events[-1]["type"] == "approval_required"
        assert not any(event["type"] == "tool_start" and event["payload"].get("name") == "write_file"
                       for event in events)
        approval_id = events[-1]["payload"]["approval_id"]
        approval = client.post(f"/api/runs/{run_id}/approvals/{approval_id}",
                               json={"decision": "approved"})
        assert approval.status_code == 200
        assert approval.json()["decision"] == "approved"
        events.extend(_terminal_events(client, run_id, last_event_id=events[-1]["seq"]))
        _assert_event_contract(events, run_id)
        assert any(event["type"] == "tool_start" and event["payload"].get("name") == "write_file"
                   for event in events)
        assert events[-1]["type"] == "turn_completed"


def test_denied_approval_fails_turn_without_write(live_server):
    with httpx.Client(base_url=live_server) as client:
        run_id = _start(client, "approval")
        events = []
        for event in _events(client, run_id):
            events.append(event)
            if event["type"] == "approval_required":
                break
        approval_id = events[-1]["payload"]["approval_id"]
        response = client.post(f"/api/runs/{run_id}/approvals/{approval_id}",
                               json={"decision": "denied"})
        assert response.status_code == 200
        events.extend(_terminal_events(client, run_id, last_event_id=events[-1]["seq"]))
        assert events[-1]["type"] == "turn_failed"
        assert events[-1]["payload"]["code"] == "approval_denied"
        assert not any(event["type"] == "tool_start" and event["payload"].get("name") == "write_file"
                       for event in events)


def test_approval_is_single_use_and_unknown_runs_are_not_found(live_server):
    with httpx.Client(base_url=live_server) as client:
        run_id = _start(client, "approval")
        for event in _events(client, run_id):
            if event["type"] == "approval_required":
                approval_id = event["payload"]["approval_id"]
                break
        path = f"/api/runs/{run_id}/approvals/{approval_id}"
        assert client.post(path, json={"decision": "approved"}).status_code == 200
        assert client.post(path, json={"decision": "approved"}).status_code == 409
        assert client.post(f"/api/runs/{run_id}/approvals/stale", json={"decision": "approved"}).status_code == 409
        assert client.post("/api/runs/unknown/approvals/stale", json={"decision": "approved"}).status_code == 404


def test_event_history_replays_after_cursor_and_runs_are_isolated(live_server):
    with httpx.Client(base_url=live_server) as client:
        first_run, second_run = _start(client, "full"), _start(client, "full")
        first = _terminal_events(client, first_run)
        second = _terminal_events(client, second_run)
        _assert_event_contract(first, first_run)
        _assert_event_contract(second, second_run)

        cursor = first[1]["seq"]
        replay = _terminal_events(client, first_run, last_event_id=cursor)
        assert [event["seq"] for event in replay] == [event["seq"] for event in first if event["seq"] > cursor]
        assert all(event["session_id"] == first_run for event in replay)
        assert all(event["session_id"] == second_run for event in second)
        assert first_run != second_run


def test_cross_origin_run_and_approval_mutations_are_rejected(live_server):
    with httpx.Client(base_url=live_server) as client:
        config = client.get("/api/config").json()
        rejected_run = client.post(
            "/api/runs",
            json={"prompt": config["demo_prompt"], "mode": "approval"},
            headers={"Origin": "https://attacker.example"},
        )
        assert rejected_run.status_code == 403

        run_id = _start(client, "approval")
        for event in _events(client, run_id):
            if event["type"] == "approval_required":
                approval_id = event["payload"]["approval_id"]
                break
        path = f"/api/runs/{run_id}/approvals/{approval_id}"
        rejected_approval = client.post(
            path,
            json={"decision": "approved"},
            headers={"Origin": "https://attacker.example"},
        )
        assert rejected_approval.status_code == 403
        accepted = client.post(path, json={"decision": "approved"})
        assert accepted.status_code == 200


@pytest.mark.parametrize("live_server", [0.3], indirect=True)
def test_expired_approval_fails_without_writing(live_server):
    with httpx.Client(base_url=live_server) as client:
        run_id = _start(client, "approval")
        events = _terminal_events(client, run_id)

        assert any(event["type"] == "approval_required" for event in events)
        assert events[-1]["type"] == "turn_failed"
        assert events[-1]["payload"]["code"] == "approval_timeout"
        assert not any(event["type"] == "tool_start" and event["payload"].get("name") == "write_file"
                       for event in events)
