import json
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import OpenAI

from app.common.model.runtime import ModelResponse, ToolCall
from app.core.provider import FakeProvider
from app.tools.builtin import BuiltinTools
from app.tools.registry import create_builtin_registry
from app.web import runtime
from app.web.api import create_app


def client_for(workspace, factory=None, *, offline=True):
    return TestClient(create_app(workspace, offline=offline, fake_provider_factory=factory),
                      base_url="http://127.0.0.1:8080")


def tool_call(name, arguments, call_id="call-1"):
    return ToolCall(call_id, name, json.dumps(arguments))


def test_static_runtime_and_registry_directory(tmp_path):
    with client_for(tmp_path) as client:
        response = client.get("/")
        assert response.history[0].headers["location"] == "/html/index.html?mode=web"
        assert response.status_code == 200 and "../js/app.js" in response.text
        for resource in ("/html/sidebar.html", "/css/index.css", "/js/app.js"):
            assert client.get(resource).status_code == 200
        for resource in ("/README.md", "/.env", "/static/html/index.html", "/html/../README.md"):
            assert client.get(resource).status_code == 404
        assert client.get("/api/runtime").json() == {
            "provider_mode": "offline",
            "capabilities": {"single_turn": True, "tool_policy": "read_only",
                             "event_delivery": "with_result", "session_resume": False},
        }
        tools = client.get("/api/tools").json()["tools"]
        assert [item["schema"] for item in tools] == create_builtin_registry(tmp_path).schemas
        assert {item["schema"]["function"]["name"] for item in tools if item["enabled"]} == {
            "read_file", "list_files",
        }


def test_read_tool_through_actual_agent_and_safe_event_projection(tmp_path, monkeypatch):
    secret = "provider-key-for-testing"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("BASE_URL", "https://provider.invalid/endpoint")
    (tmp_path / "hello.txt").write_text(f"hello from disk: {secret}", encoding="utf-8")
    provider = FakeProvider([
        ModelResponse(content="private intermediary", reasoning_content="private reasoning",
                      tool_calls=[tool_call("read_file", {"file_path": "hello.txt"})], tokens=3),
        ModelResponse(content=f"Done: {secret}\n{runtime.WEB_PROMPT}\nhttps://provider.invalid/endpoint",
                      tokens=7),
    ])
    with client_for(tmp_path, lambda: provider) as client:
        response = client.post("/api/runs", json={"prompt": f"Read hello.txt {secret}"},
                               headers={"Origin": "http://127.0.0.1:8080", "Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    result, events = (response.json()[field] for field in ("result", "events"))
    assert result == {"run_id": result["run_id"], "success": True, "response": "Done: [REDACTED]\n[REDACTED]\n[REDACTED]",
                      "tokens": 10, "tool_calls": 1, "iterations": 2, "error": None}
    assert [event["kind"] for event in events] == [
        "user_query", "model_response", "tool_start", "tool_end", "model_response", "agent_end",
    ]
    assert [event["seq"] for event in events] == list(range(1, 7))
    assert {event["run_id"] for event in events} == {result["run_id"]}
    assert events[3]["name"] == "read_file" and events[3]["status"] == "success"
    assert json.loads(provider.requests[1]["messages"][-1]["content"])["result"] == "hello from disk: [REDACTED]"
    assert {spec["function"]["name"] for spec in provider.requests[0]["tools"]} == {"read_file", "list_files"}
    for private in (secret, runtime.WEB_PROMPT, "provider.invalid", "hello.txt", "private intermediary",
                    "private reasoning", "messages", "raw_arguments", "hello from disk"):
        assert private not in response.text


def test_default_offline_provider_is_fresh_for_each_run(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "create_client", lambda: pytest.fail("Offline mode must not create a live client"))
    with client_for(tmp_path) as client:
        runs = [client.post("/api/runs", json={"prompt": prompt}) for prompt in ("First", "Second")]
    assert all(run.status_code == 200 for run in runs)
    assert len({run.json()["result"]["run_id"] for run in runs}) == 2
    assert all(any(e.get("name") == "list_files" and e.get("status") == "success"
                   for e in run.json()["events"]) for run in runs)


@pytest.mark.parametrize(("name", "arguments"), [
    ("write_file", {"file_path": "untouched.txt", "content": "new"}),
    ("run_python", {"file_path": "marker.py"}),
    ("execute_command", {"command": ["do-not-run"]}),
    ("unregistered", {}),
])
def test_non_read_attempts_stop_turn_and_have_no_side_effect(tmp_path, monkeypatch, name, arguments):
    path = tmp_path / "untouched.txt"
    path.write_text("original", encoding="utf-8")
    invoked = []

    def instrument(original):
        @wraps(original)
        def side_effect(*args, **kwargs):
            invoked.append(True)
            path.write_text("changed", encoding="utf-8")
        return side_effect

    for method in ("write_file", "run_python", "execute_command", "read_file"):
        # Keep actual signatures so the real registry still validates the tool interfaces.
        monkeypatch.setattr(BuiltinTools, method, instrument(getattr(BuiltinTools, method)))
    provider = FakeProvider([ModelResponse(tool_calls=[
        tool_call(name, arguments), tool_call("read_file", {"file_path": path.name}, "skip-me"),
    ])])
    with client_for(tmp_path, lambda: provider) as client:
        response = client.post("/api/runs", json={"prompt": "try a tool"})
    assert response.status_code == 403
    assert response.json()["result"]["error"] == "tool_denied"
    events = response.json()["events"]
    assert any(event["kind"] == "approval_result" and not event["approved"] for event in events)
    assert any(event["kind"] == "tool_skipped" for event in events)
    assert len(provider.requests) == 1 and not invoked and path.read_text(encoding="utf-8") == "original"


def test_read_failures_are_generic_even_in_model_context(tmp_path):
    provider = FakeProvider([
        ModelResponse(tool_calls=[tool_call("read_file", {"file_path": "missing-private-filename"})]),
        ModelResponse(content="Read did not succeed"),
    ])
    with client_for(tmp_path, lambda: provider) as client:
        response = client.post("/api/runs", json={"prompt": "Read missing file"})
    assert response.status_code == 200
    assert any(e.get("status") == "error" for e in response.json()["events"])
    assert "missing-private-filename" not in response.text
    assert json.loads(provider.requests[1]["messages"][-1]["content"])["result"] == "Read operation failed."


def test_provider_and_assembly_exceptions_are_not_disclosed(tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError("private error: credentials or endpoint")

    provider = FakeProvider([])
    monkeypatch.setattr(provider, "complete", fail)
    with client_for(tmp_path, lambda: provider) as client:
        failure = client.post("/api/runs", json={"prompt": "hello"})
        assert failure.status_code == 502
        assert failure.json()["result"]["error"] == "run_failed"
        assert [e["kind"] for e in failure.json()["events"]] == ["user_query", "agent_end"]
        assert "private error" not in failure.text
    with client_for(tmp_path, fail) as client:
        failure = client.post("/api/runs", json={"prompt": "hello"})
        assert failure.status_code == 500 and failure.json() == {"error": "internal_error"}
    monkeypatch.setattr(runtime, "create_client", fail)
    with client_for(tmp_path, offline=False) as client:
        assert client.get("/api/runtime").json()["provider_mode"] == "configured"
        failure = client.post("/api/runs", json={"prompt": "hello"})
        assert failure.status_code == 503 and failure.json() == {"error": "provider_unavailable"}


def test_configured_mode_uses_existing_provider_over_mock_transport(tmp_path, monkeypatch):
    calls = []

    def complete(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Mock answer"}}],
            "usage": {"total_tokens": 11},
        })

    sdk = OpenAI(api_key="test-key", base_url="https://provider.invalid/v1",
                 http_client=httpx.Client(transport=httpx.MockTransport(complete)))
    monkeypatch.setattr(runtime, "create_client", lambda: sdk)
    with client_for(tmp_path, offline=False) as client:
        response = client.post("/api/runs", json={"prompt": "  hello  "})
    assert response.status_code == 200 and response.json()["result"]["response"] == "Mock answer"
    assert response.json()["result"]["tokens"] == 11 and sdk.is_closed()
    assert len(calls) == 1 and calls[0]["messages"][-1] == {"role": "user", "content": "hello"}


@pytest.mark.parametrize("payload", [{}, {"prompt": "  "}, {"prompt": 3}, {"prompt": "x" * 8193},
                                     {"prompt": "ok", "workspace": "private"},
                                     {"prompt": "ok", "api_key": "private"},
                                     {"prompt": "ok", "base_url": "private"}])
def test_invalid_payload_does_not_reach_provider(tmp_path, payload):
    with client_for(tmp_path, lambda: pytest.fail("Invalid request reached Provider")) as client:
        response = client.post("/api/runs", json=payload)
    assert response.status_code == 422 and response.json() == {"error": "invalid_request"}


def test_origin_host_content_type_and_query_validation(tmp_path):
    with client_for(tmp_path, lambda: pytest.fail("Rejected request reached Provider")) as client:
        for headers, code in [({"Host": "not-local.invalid"}, "invalid_host"),
                              ({"Origin": "http://127.0.0.1:8765"}, "cross_origin"),
                              ({"Origin": "http://localhost:8080"}, "cross_origin"),
                              ({"Origin": "null"}, "cross_origin"),
                              ({"Sec-Fetch-Site": "cross-site"}, "cross_origin")]:
            response = client.post("/api/runs", json={"prompt": "ok"}, headers=headers)
            assert response.status_code == 403 and response.json() == {"error": code}
        response = client.post("/api/runs", data={"prompt": "ok"})
        assert response.status_code == 415 and response.json() == {"error": "json_required"}
        for response in (client.post("/api/runs?workspace=private", json={"prompt": "ok"}),
                         client.post("/api/runs", content='{"private":', headers={"Content-Type": "application/json"})):
            assert response.status_code == 422 and response.json() == {"error": "invalid_request"}


def test_blocking_model_does_not_block_asgi_loop(tmp_path):
    started, release = Event(), Event()
    provider = FakeProvider([ModelResponse(content="done")])
    complete = provider.complete

    def blocking_complete(*args):
        started.set()
        if not release.wait(5):
            raise TimeoutError
        return complete(*args)

    provider.complete = blocking_complete
    with client_for(tmp_path, lambda: provider) as client, ThreadPoolExecutor(max_workers=2) as workers:
        model_call = workers.submit(client.post, "/api/runs", json={"prompt": "wait"})
        try:
            assert started.wait(3)
            capabilities = workers.submit(client.get, "/api/runtime")
            assert capabilities.result(timeout=3).status_code == 200
            assert not model_call.done()
        finally:
            release.set()
        assert model_call.result(timeout=3).status_code == 200