"""Small local SSE prototype for the Toy Harness."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.common.api.tool import ToolSpec
from app.common.event.agent_event import AgentEvent
from app.common.model.runtime import ModelResponse
from app.common.security import redact_values
from app.tools.registry import create_async_builtin_registry, validate_arguments

ROOT = Path(__file__).resolve().parents[2]
ALLOWED_TOOLS = frozenset({"read_file", "list_files", "write_file"})
MAX_ACTIVE_RUNS = 20
MAX_EVENTS = 500
MAX_RETAINED_RUNS = 100
MAX_ITERATIONS = 15
APPROVAL_TIMEOUT = 60.0


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    mode: str


class ApprovalRequestBody(BaseModel):
    decision: str


class Run:
    def __init__(self, run_id: str, prompt: str, mode: str):
        self.id, self.prompt, self.mode = run_id, prompt, mode
        self.events: list[dict[str, Any]] = []
        self.seq = 0
        self.condition = asyncio.Condition()
        self.pending: dict[str, tuple[str, asyncio.Future[str]]] = {}
        self.terminal = False
        self.task: asyncio.Task | None = None
        self.created = time.monotonic()
        self.workspace: tempfile.TemporaryDirectory | None = None

    async def emit(self, kind: str, **payload: Any) -> None:
        self.seq += 1
        event = AgentEvent(kind, self.id, self.seq, redact_values(payload)).to_dict()
        async with self.condition:
            self.events.append(event)
            if len(self.events) > MAX_EVENTS:
                del self.events[:len(self.events) - MAX_EVENTS]
            self.condition.notify_all()


class RunManager:
    def __init__(self, approval_timeout: float = APPROVAL_TIMEOUT):
        self.runs: dict[str, Run] = {}
        self.lock = asyncio.Lock()
        self.approval_timeout = approval_timeout

    async def start(self, prompt: str, mode: str) -> Run:
        if mode not in {"approval", "full"}:
            raise HTTPException(422, "mode must be 'approval' or 'full'")
        async with self.lock:
            self._prune()
            if sum(not run.terminal for run in self.runs.values()) >= MAX_ACTIVE_RUNS:
                raise HTTPException(429, "too many active runs")
            run = Run(uuid4().hex, prompt, mode)
            self.runs[run.id] = run
            run.task = asyncio.create_task(self._execute(run), name=f"web-run-{run.id}")
            return run

    def _prune(self) -> None:
        now = time.monotonic()
        for key, run in list(self.runs.items()):
            if run.terminal and now - run.created > 300:
                self.runs.pop(key, None)
        if len(self.runs) > MAX_RETAINED_RUNS:
            for key, run in list(self.runs.items()):
                if run.terminal:
                    self.runs.pop(key, None)
                    if len(self.runs) <= MAX_RETAINED_RUNS:
                        break

    async def shutdown(self) -> None:
        tasks = []
        for run in self.runs.values():
            if run.task and not run.task.done():
                run.task.cancel()
                tasks.append(run.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for run in self.runs.values():
            if run.workspace is not None:
                run.workspace.cleanup()
                run.workspace = None

    async def _execute(self, run: Run) -> None:
        from app.web.providers import DEMO_INITIAL_CONTENT, open_provider, provider_name

        try:
            run.workspace = tempfile.TemporaryDirectory(prefix="toy-harness-web-")
            (Path(run.workspace.name) / "notes.txt").write_text(DEMO_INITIAL_CONTENT, encoding="utf-8")
            registry = create_async_builtin_registry(run.workspace.name)
            # Build a filtered model-facing schema list; execution still checks the explicit allowlist.
            tools = [schema for schema in registry.schemas
                     if schema.get("function", {}).get("name") in ALLOWED_TOOLS]
            schemas = {item["function"]["name"]: item["function"]["parameters"] for item in tools}
            provider_id = provider_name()
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": "You are a careful assistant. Use only the provided file tools."},
                {"role": "user", "content": run.prompt},
            ]
            await run.emit("user_message", content=run.prompt)
            total_tokens = 0
            used_call_ids: set[str] = set()
            for iteration in range(1, MAX_ITERATIONS + 1):
                await run.emit("model_request", iteration=iteration, provider=provider_id)
                with open_provider(provider_id) as provider:
                    response: ModelResponse = await asyncio.to_thread(provider.complete, messages, tools)
                total_tokens += response.tokens
                serial_calls = [call.to_message() for call in response.tool_calls]
                await run.emit("model_response", iteration=iteration, content=response.content,
                               finish_reason=response.finish_reason,
                               tool_calls=[{"id": call.id, "name": call.name}
                                           for call in response.tool_calls])
                if response.finish_reason in {"length", "content_filter"}:
                    await run.emit("turn_failed", error="Incomplete model response", code="incomplete_response")
                    return
                if not response.tool_calls:
                    await run.emit("turn_completed", iterations=iteration, tokens=total_tokens,
                                   content=response.content or "")
                    return
                messages.append({"role": "assistant", "content": response.content,
                                 "tool_calls": serial_calls})
                if response.reasoning_content is not None:
                    messages[-1]["reasoning_content"] = response.reasoning_content
                for call in response.tool_calls:
                    if not call.id or call.id in used_call_ids:
                        raise ValueError("Tool call IDs must be non-empty and unique within a run")
                    used_call_ids.add(call.id)
                    try:
                        arguments = json.loads(call.arguments)
                        if not isinstance(arguments, dict):
                            raise ValueError("Tool arguments must be an object")
                    except (TypeError, ValueError) as exc:
                        reason, result_data = f"Invalid tool arguments: {type(exc).__name__}", "Invalid tool arguments."
                    else:
                        reason = None
                        if call.name not in ALLOWED_TOOLS:
                            reason = "Tool is not available in the web prototype."
                        else:
                            try:
                                validate_arguments(arguments, schemas[call.name])
                            except (TypeError, ValueError) as exc:
                                reason = f"Invalid arguments: {type(exc).__name__}"
                        result_data = reason or ""
                    if reason:
                        await run.emit("tool_skipped", call_id=call.id, name=call.name, reason=reason)
                        result_status = "error"
                    else:
                        if call.name == "write_file" and run.mode == "approval":
                            approval_id = uuid4().hex
                            future = asyncio.get_running_loop().create_future()
                            run.pending[approval_id] = (call.id, future)
                            await run.emit("approval_required", approval_id=approval_id, call_id=call.id,
                                           tool_name=call.name, arguments=arguments,
                                           description=f"Write {arguments.get('file_path', '')}")
                            try:
                                decision = await asyncio.wait_for(future, timeout=self.approval_timeout)
                            except asyncio.TimeoutError:
                                run.pending.pop(approval_id, None)
                                await run.emit("approval_decided", approval_id=approval_id,
                                               call_id=call.id, decision="timeout", actor="system")
                                await run.emit("turn_failed", error="Approval timed out", code="approval_timeout")
                                return
                            await run.emit("approval_decided", approval_id=approval_id,
                                           call_id=call.id, decision=decision, actor="user")
                            if decision == "denied":
                                await run.emit("turn_failed", error="Write approval denied", code="approval_denied")
                                return
                        await run.emit("tool_start", iteration=iteration, call_id=call.id,
                                       name=call.name, arguments=arguments)
                        started = time.monotonic()
                        result = await registry.execute(call.id, call.name, arguments)
                        result_status, result_data = result.status, result.data
                        extra = {"approved": True} if call.name == "write_file" and run.mode == "approval" else {}
                        await run.emit("tool_end", call_id=call.id, name=call.name,
                                       status=result_status, result=result_data,
                                       elapsed_ms=int((time.monotonic() - started) * 1000), **extra)
                    messages.append({"role": "tool", "tool_call_id": call.id,
                                     "content": json.dumps({"status": result_status, "result": result_data}, ensure_ascii=False)})
            await run.emit("turn_failed", error="Maximum iterations reached", code="max_iterations")
        except Exception as exc:
            await run.emit("turn_failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            try:
                for _, future in run.pending.values():
                    if not future.done():
                        future.cancel()
                run.pending.clear()
                if run.workspace is not None:
                    run.workspace.cleanup()
                    run.workspace = None
            finally:
                run.terminal = True
                # Wake SSE subscribers so terminal streams close promptly.
                async with run.condition:
                    run.condition.notify_all()

    async def decide(self, run: Run, approval_id: str, decision: str) -> None:
        pending = run.pending.get(approval_id)
        if decision not in {"approved", "denied"} or pending is None or pending[1].done():
            raise HTTPException(409, "approval is stale or already decided")
        run.pending.pop(approval_id, None)
        pending[1].set_result(decision)


def create_app(*, approval_timeout: float = APPROVAL_TIMEOUT) -> FastAPI:
    manager = RunManager(approval_timeout=approval_timeout)
    @asynccontextmanager
    async def lifespan(_app):
        yield
        await manager.shutdown()

    app = FastAPI(title="Toy Harness Web Prototype", lifespan=lifespan)
    app.state.run_manager = manager
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "::1", "testserver"])
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.get("/")
    async def home():
        return FileResponse(ROOT / "static" / "html" / "index.html")

    @app.get("/api/config")
    async def config():
        from app.web.providers import DEMO_PROMPT, provider_name
        return {"provider": provider_name(), "demo_prompt": DEMO_PROMPT}

    def check_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if not origin:
            return
        from urllib.parse import urlsplit

        parsed = urlsplit(origin)
        actual = request.url
        if (parsed.scheme.lower(), parsed.netloc.lower()) != (actual.scheme.lower(), actual.netloc.lower()):
            raise HTTPException(403, "cross-origin requests are not allowed")

    @app.post("/api/runs", status_code=202)
    async def start_run(body: RunRequest, request: Request):
        check_origin(request)
        run = await manager.start(body.prompt, body.mode)
        return {"run_id": run.id}

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request):
        run = manager.runs.get(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        try:
            last_id = max(0, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            last_id = 0
        if run.events and last_id < run.events[0]["seq"] - 1:
            raise HTTPException(410, "Requested event history has expired")

        async def stream():
            cursor = last_id
            while True:
                keepalive = False
                async with run.condition:
                    available = [event for event in run.events if event["seq"] > cursor]
                    if not available and not run.terminal:
                        try:
                            await asyncio.wait_for(run.condition.wait(), timeout=15)
                        except asyncio.TimeoutError:
                            if await request.is_disconnected():
                                return
                            keepalive = True
                        if not keepalive:
                            available = [event for event in run.events if event["seq"] > cursor]
                if available and available[0]["seq"] != cursor + 1:
                    # Reconnecting with the last delivered ID reports expired history.
                    return
                if keepalive:
                    yield b": keepalive\n\n"
                    continue
                for event in available:
                    cursor = event["seq"]
                    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {data}\n\n".encode()
                if run.terminal and cursor >= (run.events[-1]["seq"] if run.events else 0):
                    return

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/{run_id}/approvals/{approval_id}")
    async def approval(run_id: str, approval_id: str, body: ApprovalRequestBody, request: Request):
        check_origin(request)
        run = manager.runs.get(run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        await manager.decide(run, approval_id, body.decision)
        return {"decision": body.decision}

    return app


app = create_app()
