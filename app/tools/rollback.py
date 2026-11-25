import asyncio
import hashlib
import json
from typing import Sequence

from app.common.api.audit_service import AuditService
from app.common.api.rollback_service import CheckpointStore, CheckpointView, RollbackResult
from app.common.api.state_store import MessageRole, StateStore
from app.common.api.tool_registry import ToolRegistry
from app.common.exception import NotFoundError, ToolError
from app.common.security import redact
from app.tools.file_write import FileWrite


class DefaultRollbackService:
    def __init__(
        self, store: StateStore, checkpoints: CheckpointStore, tools: ToolRegistry, audit: AuditService,
    ):
        self.store = store
        self.checkpoints = checkpoints
        self.tools = tools
        self.audit = audit
        self._lock = asyncio.Lock()

    async def create_checkpoint(
        self, session_id: str, description: str | None = None,
    ) -> CheckpointView:
        seq = await self.store.latest_message_seq(session_id)
        return await self.checkpoints.save_checkpoint(session_id, seq=seq, description=description)

    async def list_checkpoints(self, session_id: str) -> Sequence[CheckpointView]:
        return await self.checkpoints.list_checkpoints(session_id)

    async def rollback(self, session_id: str, checkpoint_id: str) -> RollbackResult:
        async with self._lock:
            checkpoint = await self.checkpoints.load_checkpoint(checkpoint_id)
            if checkpoint is None or checkpoint.session_id != session_id:
                raise NotFoundError(f"Checkpoint {checkpoint_id} was not found in this session")
            calls = {call.id: call for call in await self.store.list_tool_calls(session_id) if call.reversible}
            messages = await self.store.list_messages(session_id, after_seq=checkpoint.seq)
            eligible = {
                message.tool_call_id: calls[message.tool_call_id]
                for message in sorted(messages, key=lambda entry: entry.seq, reverse=True)
                if message.role == MessageRole.TOOL and message.tool_call_id in calls
            }
            # shortcut: the local store can scan session audit history; add a targeted query if it grows.
            journal = {}
            if eligible:
                actions = await self.audit.query(session_id=session_id, limit=2**63 - 1)
                for action in actions:
                    if action.actor == "harness" and action.action in {
                        "rollback_started", "rollback_compensated", "rollback_call_failed",
                    }:
                        marker = next(iter(json.loads(action.detail)["call_marker"]))
                        journal[marker] = action.action

            reverted = []
            failed = []
            blocked_targets = set()
            for call in eligible.values():
                marker = hashlib.sha256(call.id.encode("utf-8")).hexdigest()
                previous_action = journal.get(marker)
                if previous_action == "rollback_compensated":
                    continue
                receipt = await self.store.get_receipt(call.id)
                try:
                    if receipt is None:
                        raise ToolError("Missing rollback receipt")
                    tool = self.tools.get(call.tool_name)
                    target = tool.rollback_target(receipt) if isinstance(tool, FileWrite) else None
                except (ToolError, KeyError, TypeError, ValueError) as error:
                    failed.append((call.id, redact(error)))
                    blocked_targets.add(None)
                    continue
                if None in blocked_targets or target in blocked_targets or (target is None and blocked_targets):
                    failed.append((call.id, "Skipped after a later operation on the same resource failed"))
                    continue

                details = {"call_id": call.id, "checkpoint_id": checkpoint.id, "call_marker": {marker: True}}
                retry = previous_action == "rollback_started"
                if retry and not isinstance(tool, FileWrite):
                    failed.append((call.id, "Previous rollback attempt has no recorded outcome"))
                    blocked_targets.add(target)
                    continue
                if not retry:
                    await self.audit.log(action="rollback_started", actor="harness", session_id=session_id, detail=details)
                try:
                    if isinstance(tool, FileWrite):
                        result = await tool.rollback(receipt, retry=retry)
                    else:
                        result = await tool.rollback(receipt)
                    failure = None if result.status == "success" else redact(result.data)
                except Exception as error:
                    failure = redact(error)
                outcome = "rollback_compensated" if failure is None else "rollback_call_failed"
                await self.audit.log(action=outcome, actor="harness", session_id=session_id, detail=details)
                if failure is None:
                    reverted.append(call.id)
                else:
                    failed.append((call.id, failure))
                    blocked_targets.add(target)

            await self.audit.log(
                action="rollback_performed", actor="harness", session_id=session_id,
                detail={"checkpoint_id": checkpoint.id, "reverted": reverted, "failed": failed},
            )
            return RollbackResult(checkpoint, tuple(reverted), tuple(failed))
