import json
import os
import stat
from dataclasses import dataclass

import pytest

from app.common.api.llm_provider import ModelResponse, ModelToolCall
from app.common.api.state_store import ApprovalDecision, ApprovalStatus, MessageRole, ToolCallStatus
from app.common.api.tool import ToolResult, ToolSpec
from app.common.exception import ApprovalError, NotFoundError, StateError, ToolError
from app.tools import file_write
from app.tools.approval import DefaultApprovalService
from app.tools.audit import DefaultAuditService
from app.tools.registry import DefaultToolRegistry, create_async_builtin_registry
from app.tools.rollback import DefaultRollbackService
from tests.fakes.memory_state_store import MemoryStateStore


@dataclass
class Checkpoint:
    id: str
    session_id: str
    seq: int
    description: str | None
    state_json: str | None = None
    created_at: int = 0


class Checkpoints:
    def __init__(self):
        self.saved = {}

    async def save_checkpoint(self, session_id, *, seq, description=None):
        checkpoint = Checkpoint(str(len(self.saved)), session_id, seq, description)
        self.saved[checkpoint.id] = checkpoint
        return checkpoint

    async def load_checkpoint(self, checkpoint_id):
        return self.saved.get(checkpoint_id)

    async def list_checkpoints(self, session_id):
        return [item for item in self.saved.values() if item.session_id == session_id]


async def add_write(store, session_id, call_id, arguments):
    await store.record_assistant_turn(session_id, ModelResponse(tool_calls=[
        ModelToolCall(id=call_id, name="write_file", arguments=json.dumps(arguments)),
    ]))
    return next(call for call in await store.list_tool_calls(session_id) if call.id == call_id)


async def dispatch_approved(store, tools, call):
    # Exercise the shared state contract that the Agent loop uses to claim a tool call.
    decision = await store.get_approval_for_call(call.id)
    if decision is None or decision.status != ApprovalStatus.APPROVED:
        return None
    if not await store.claim_tool_call(
        call.id, from_status=ToolCallStatus.AWAITING_APPROVAL, to_status=ToolCallStatus.APPROVED,
    ):
        return None
    if not await store.claim_tool_call(
        call.id, from_status=ToolCallStatus.APPROVED, to_status=ToolCallStatus.DISPATCHED,
    ):
        return None
    result = await tools.execute(call.id, call.tool_name, json.loads(call.arguments))
    await store.finish_tool_call(call.id, result)
    return result


async def test_registry_reuses_builtins_and_validates_before_execution(tmp_path):
    tools = create_async_builtin_registry(tmp_path)
    file = tmp_path / "例子.txt"
    file.write_text("原文", encoding="utf-8")
    assert (await tools.execute("read", "read_file", {"file_path": file.name})).data == "原文"
    assert (await tools.execute("invalid", "write_file", {"file_path": file.name})).status == "error"
    assert (await tools.execute("outside", "write_file", {"file_path": "../例子.txt", "content": "bad"})).status == "error"
    assert file.read_text(encoding="utf-8") == "原文"
    original = tools.schemas
    tools.schemas[0]["function"]["name"] = "changed"
    assert tools.schemas == original
    with pytest.raises(ToolError):
        tools.register(tools.get("read_file"))
    with pytest.raises(ToolError):
        await tools.execute("unknown", "missing_tool", {})


async def test_registry_redacts_exception_details(monkeypatch):
    secret = 'sample-"private\\detail'
    monkeypatch.setenv("TEST_API_KEY", secret)

    class BrokenTool:
        spec = ToolSpec("broken", "Test failure", {"type": "object"})

        async def execute(self, arguments):
            raise ValueError(f"Failed with {secret}")

    tools = DefaultToolRegistry()
    tools.register(BrokenTool())
    result = await tools.execute("broken-call", "broken", {})
    assert result.status == "error"
    assert result.data.endswith("Failed with [REDACTED]")


async def test_policy_and_audit_for_approval_and_rejection(tmp_path, monkeypatch):
    store = MemoryStateStore()
    decisions = DefaultApprovalService(store)
    audit_service = DefaultAuditService(store)
    tools = create_async_builtin_registry(tmp_path)
    session = await store.create_session()
    await store.append_message(session.id, MessageRole.USER, "Write to a file")
    file = tmp_path / "notes.txt"
    file.write_bytes(b"old")
    secret = 'sample-"private\\value'
    monkeypatch.setenv("TEST_API_KEY", secret)
    assert not decisions.requires_approval("read_file", {"file_path": file.name})
    assert decisions.requires_approval("execute_command", {"command": ["echo", "example"]})
    assert decisions.requires_approval("unknown_plugin", {})
    assert decisions.requires_approval("write_file", {"file_path": file.name, "content": secret})

    call = await add_write(store, session.id, "yes", {"file_path": file.name, "content": secret})
    request = await decisions.request(call)
    assert (await decisions.request(call)).id == request.id
    assert [item.id for item in await decisions.pending(session.id)] == [request.id]
    assert await dispatch_approved(store, tools, call) is None
    assert file.read_bytes() == b"old"
    await decisions.decide(request.id, ApprovalDecision.APPROVED, actor="reviewer")
    assert (await decisions.find_for_call(call.id)).id == request.id
    result = await dispatch_approved(store, tools, call)
    assert result.status == "success"
    assert await dispatch_approved(store, tools, call) is None
    await audit_service.log(action="tool_finished", actor="agent", session_id=session.id,
                            detail={"call_id": call.id, "status": result.status, "value": secret})
    with pytest.raises(ApprovalError):
        await decisions.decide(request.id, ApprovalDecision.DENIED, actor="reviewer")

    denied = await add_write(store, session.id, "no", {"file_path": file.name, "content": "blocked"})
    request = await decisions.request(denied)
    await decisions.decide(request.id, ApprovalDecision.DENIED, actor="reviewer")
    assert await dispatch_approved(store, tools, denied) is None
    await store.finish_tool_call(denied.id, ToolResult("denied"))
    assert file.read_text(encoding="utf-8") == secret
    assert not await decisions.pending(session.id)
    records = await audit_service.query(session_id=session.id)
    assert [item.action for item in records] == ["approval_decided", "tool_finished", "approval_decided"]
    assert records[0].actor == "reviewer"
    assert json.loads(records[1].detail)["value"] == "[REDACTED]"


@pytest.mark.parametrize(
    "scenario", ["normal", "external-edit", "same-bytes-edit", "audit-interruption", "redacted-call-id"],
)
async def test_rollback_uses_message_order_and_restores_original_bytes(tmp_path, monkeypatch, scenario):
    store = MemoryStateStore()
    checkpoints = Checkpoints()
    decisions = DefaultApprovalService(store)
    audit_service = DefaultAuditService(store)
    tools = create_async_builtin_registry(tmp_path)
    service = DefaultRollbackService(store, checkpoints, tools, audit_service)
    session = await store.create_session()
    await store.append_message(session.id, MessageRole.USER, "Write two versions")
    file = tmp_path / "notes.txt"
    original = "原始\r\n内容\n".encode()
    file.write_bytes(original)
    second_id = "sk-" + "x" * 16 if scenario == "redacted-call-id" else "second"
    calls = [
        ModelToolCall(id=call_id, name="write_file", arguments=json.dumps({"file_path": file.name, "content": text}))
        for call_id, text in [("first", "第一次"), (second_id, "第二次")]
    ]
    await store.record_assistant_turn(session.id, ModelResponse(tool_calls=calls))
    checkpoint = await service.create_checkpoint(session.id, "before writing")
    for call in await store.list_tool_calls(session.id):
        request = await decisions.request(call)
        await decisions.decide(request.id, ApprovalDecision.APPROVED, actor="reviewer")
        result = await dispatch_approved(store, tools, call)
        assert result.receipt and result.receipt == await store.get_receipt(call.id)
        assert "receipt" not in result.to_dict()
    assert file.read_text(encoding="utf-8") == "第二次"
    history_count = len(await store.list_messages(session.id))
    assert (await service.list_checkpoints(session.id))[0] == checkpoint
    # Rebuild services before rollback so the receipt must survive beyond the
    # tool instance that created it.
    service = DefaultRollbackService(
        store, checkpoints, create_async_builtin_registry(tmp_path), audit_service,
    )
    with pytest.raises(NotFoundError):
        await service.rollback((await store.create_session()).id, checkpoint.id)
    if scenario in {"external-edit", "same-bytes-edit"}:
        external_version = "第一次" if scenario == "external-edit" else "第二次"
        modified_at = file.stat().st_mtime_ns + 2_000_000_000
        file.write_bytes(external_version.encode())
        os.utime(file, ns=(modified_at, modified_at))
        result = await service.rollback(session.id, checkpoint.id)
        assert not result.ok
        assert [call_id for call_id, reason in result.failed] == ["second", "first"]
        assert file.read_bytes() == external_version.encode()
        assert file.stat().st_mtime_ns == modified_at
        record = (await audit_service.query(session_id=session.id))[-1]
        assert len(json.loads(record.detail)["failed"]) == 2
        return
    if scenario == "audit-interruption":
        log = audit_service.log

        async def interrupt_after_restore(**event):
            if event["action"] == "rollback_compensated":
                raise StateError("audit unavailable")
            return await log(**event)

        with monkeypatch.context() as failing:
            failing.setattr(audit_service, "log", interrupt_after_restore)
            with pytest.raises(StateError, match="audit unavailable"):
                await service.rollback(session.id, checkpoint.id)
            assert file.read_bytes() == "第一次".encode()
    result = await service.rollback(session.id, checkpoint.id)
    assert result.ok and list(result.reverted) == [second_id, "first"]
    assert file.read_bytes() == original
    assert len(await store.list_messages(session.id)) == history_count
    record = (await audit_service.query(session_id=session.id))[-1]
    assert record.action == "rollback_performed"
    visible_id = "[REDACTED]" if scenario == "redacted-call-id" else second_id
    assert json.loads(record.detail)["reverted"] == [visible_id, "first"]
    restarted = DefaultRollbackService(store, checkpoints, create_async_builtin_registry(tmp_path), audit_service)
    repeated = await restarted.rollback(session.id, checkpoint.id)
    assert repeated.ok and not repeated.reverted
    assert file.read_bytes() == original


async def test_one_conflict_does_not_prevent_rolling_back_another_file(tmp_path):
    store = MemoryStateStore()
    decisions = DefaultApprovalService(store)
    tools = create_async_builtin_registry(tmp_path)
    service = DefaultRollbackService(store, Checkpoints(), tools, DefaultAuditService(store))
    session = await store.create_session()
    await store.append_message(session.id, MessageRole.USER, "Write two files")
    checkpoint = await service.create_checkpoint(session.id)
    for call_id in ["safe", "conflict"]:
        path = tmp_path / f"{call_id}.txt"
        path.write_bytes(b"original")
        call = await add_write(store, session.id, call_id, {"file_path": path.name, "content": "changed"})
        request = await decisions.request(call)
        await decisions.decide(request.id, ApprovalDecision.APPROVED, actor="reviewer")
        assert (await dispatch_approved(store, tools, call)).status == "success"

    (tmp_path / "conflict.txt").write_bytes(b"external edit")
    result = await service.rollback(session.id, checkpoint.id)
    assert list(result.reverted) == ["safe"]
    assert [call_id for call_id, reason in result.failed] == ["conflict"]
    assert (tmp_path / "safe.txt").read_bytes() == b"original"
    assert (tmp_path / "conflict.txt").read_bytes() == b"external edit"


async def test_file_compensation_preserves_external_changes_and_removes_created_file(tmp_path):
    tools = create_async_builtin_registry(tmp_path)
    file = tmp_path / "new.txt"
    first = await tools.execute("a", "write_file", {"file_path": file.name, "content": "new"})
    writer = tools.get("write_file")
    original_stat = file.stat()
    file.write_text("external", encoding="utf-8")
    assert (await writer.rollback(first.receipt)).status == "error"
    assert file.read_text(encoding="utf-8") == "external"
    file.write_text("new", encoding="utf-8")
    os.utime(file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert (await writer.rollback(first.receipt)).status == "success"
    assert not file.exists()
    assert (await writer.rollback('{"path": "../outside"}')).status == "error"


async def test_failed_replace_cleans_up_staged_file(tmp_path, monkeypatch):
    tools = create_async_builtin_registry(tmp_path)
    file = tmp_path / "read-only.txt"
    file.write_bytes(b"original")
    file.chmod(stat.S_IREAD)

    def fail_replace(source, target):
        raise PermissionError("replacement refused")

    monkeypatch.setattr(file_write.os, "replace", fail_replace)
    try:
        result = await tools.execute("write", "write_file", {"file_path": file.name, "content": "draft"})
        assert result.status == "error"
        assert file.read_bytes() == b"original"
        assert list(tmp_path.iterdir()) == [file]
    finally:
        file.chmod(stat.S_IWRITE | stat.S_IREAD)
