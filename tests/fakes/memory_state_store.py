"""StateStore 端口的内存实现，供契约测试使用。

只在 ``tests/`` 下存在，**不进入 app 包**：应用里出现第二个状态实现就会出现两份真相。

它与将来 C 的 SQLite 实现必须通过同一套 ``tests/contracts`` 用例，包括：事务回滚、
条件消费/派发、审批拒绝写入、已决策未消费的恢复、UNKNOWN 的幂等判定、补偿凭据读回。
"""

from __future__ import annotations

import copy
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Sequence
from uuid import uuid4

from app.common.api.llm_provider import ModelResponse
from app.common.api.state_store import (
    OPEN_TOOL_STATUSES,
    TERMINAL_SESSION_STATUSES,
    TOOL_TRANSITIONS,
    ApprovalDecision,
    ApprovalStatus,
    AssistantTurn,
    MessageRole,
    SessionStatus,
    ToolCallStatus,
    TurnState,
    TurnStatus,
)
from app.common.api.tool import ToolResult
from app.common.exception import (
    ApprovalError,
    ConflictError,
    NotFoundError,
    StateError,
)

_CLAIM_TARGETS = frozenset({ToolCallStatus.APPROVED, ToolCallStatus.DISPATCHED})
_NON_TAIL_ROLES = frozenset({MessageRole.SUMMARY, MessageRole.SYSTEM})
_REOPENING_ROLES = frozenset({MessageRole.USER, MessageRole.TOOL})


def _now() -> int:
    return int(time.time() * 1000)


def _iteration(messages: Sequence[_Message]) -> int:
    """本轮已发生的模型调用次数：最后一条 user 消息之后的 assistant 条数。"""
    start = 0
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == MessageRole.USER:
            start = index + 1
            break
    return sum(1 for message in messages[start:] if message.role == MessageRole.ASSISTANT)


def _observation_payload(result: ToolResult) -> str:
    """先序列化再动状态：序列化失败时调用状态与消息都保持原样。"""
    try:
        return json.dumps(
            {"status": result.status, "result": result.data, "receipt": result.receipt},
            ensure_ascii=False,
        )
    except (TypeError, ValueError) as error:
        raise StateError(f"工具结果无法序列化，调用状态保持不变: {error}") from error


@dataclass
class _Session:
    id: str
    title: str | None
    status: str
    agents_md_version: str | None
    prefix_hash: str | None
    created_at: int
    updated_at: int


@dataclass
class _Message:
    id: str
    session_id: str
    seq: int
    role: str
    content: str | None
    tool_call_id: str | None
    created_at: int


@dataclass
class _ToolCall:
    id: str
    session_id: str
    message_id: str | None
    tool_name: str
    arguments: str | None
    status: str
    reversible: bool | None
    created_at: int


@dataclass
class _Approval:
    id: str
    session_id: str
    call_id: str
    tool_name: str
    arguments: str | None
    description: str | None
    status: str
    decided_by: str | None
    created_at: int
    decided_at: int | None


@dataclass
class _Audit:
    id: str
    session_id: str | None
    actor: str
    action: str
    detail: str | None
    created_at: int


class MemoryStateStore:
    """满足 ``app.common.api.state_store.StateStore`` 的最小实现。"""

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._messages: dict[str, list[_Message]] = {}
        self._calls: dict[str, _ToolCall] = {}
        self._observations: dict[str, str] = {}
        self._approvals: dict[str, _Approval] = {}
        self._audits: list[_Audit] = []

    # --- 会话 ---

    async def create_session(
        self, *, title: str | None = None, agents_md_version: str | None = None
    ) -> _Session:
        now = _now()
        session = _Session(
            id=uuid4().hex,
            title=title,
            status=SessionStatus.IDLE.value,
            agents_md_version=agents_md_version,
            prefix_hash=None,
            created_at=now,
            updated_at=now,
        )
        self._sessions[session.id] = session
        self._messages[session.id] = []
        return session

    async def load_session(self, session_id: str) -> _Session | None:
        return self._sessions.get(session_id)

    async def update_session(self, session_id: str, **fields: Any) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise NotFoundError(f"session {session_id} not found")
        if session.status in {status.value for status in TERMINAL_SESSION_STATUSES}:
            raise StateError(f"session {session_id} is {session.status}; terminal state is immutable")
        for key, value in fields.items():
            if key == "id":
                raise StateError("session id is immutable")
            if not hasattr(session, key):
                raise StateError(f"unknown session field: {key}")
            if key == "status":
                try:
                    value = SessionStatus(value).value
                except ValueError as error:
                    raise StateError(f"unknown session status: {value!r}") from error
            setattr(session, key, value)
        session.updated_at = _now()
        return session

    async def list_sessions(self, limit: int = 50) -> Sequence[_Session]:
        ordered = sorted(self._sessions.values(), key=lambda s: s.updated_at, reverse=True)
        return ordered[:limit]

    # --- 消息 ---

    async def append_message(
        self,
        session_id: str,
        role: MessageRole | str,
        content: str | None,
        *,
        tool_call_id: str | None = None,
    ) -> _Message:
        session = self._sessions.get(session_id)
        if session is None:
            raise NotFoundError(f"session {session_id} not found")
        if session.status in {status.value for status in TERMINAL_SESSION_STATUSES}:
            raise ConflictError(f"session {session_id} is {session.status}; cannot append message")
        try:
            normalized = MessageRole(role)
        except ValueError as error:
            raise StateError(f"unknown message role: {role!r}") from error
        messages = self._messages[session_id]
        message = _Message(
            id=uuid4().hex,
            session_id=session_id,
            seq=len(messages) + 1,
            role=normalized.value,
            content=content,
            tool_call_id=tool_call_id,
            created_at=_now(),
        )
        messages.append(message)
        return message

    async def record_assistant_turn(
        self, session_id: str, response: ModelResponse
    ) -> AssistantTurn:
        session = self._sessions.get(session_id)
        if session is None:
            raise NotFoundError(f"session {session_id} not found")
        if session.status in {status.value for status in TERMINAL_SESSION_STATUSES}:
            raise ConflictError(f"session {session_id} is {session.status}; cannot record turn")
        ids = [call.id for call in response.tool_calls]
        if len(set(ids)) != len(ids):
            raise StateError("assistant turn contains duplicate tool call ids")
        if any(identifier in self._calls for identifier in ids):
            raise StateError("assistant turn reuses an existing tool call id")
        # 校验全部通过后才开始写，保证不会留下「有消息、没调用」的半条记录。
        message = await self.append_message(
            session_id, MessageRole.ASSISTANT, response.content
        )
        calls = []
        for call in response.tool_calls:
            row = _ToolCall(
                id=call.id,
                session_id=session_id,
                message_id=message.id,
                tool_name=call.name,
                arguments=call.arguments,
                status=ToolCallStatus.PENDING.value,
                reversible=None,
                created_at=_now(),
            )
            self._calls[call.id] = row
            calls.append(row)
        return AssistantTurn(message=message, tool_calls=tuple(calls))

    async def list_messages(self, session_id: str, *, after_seq: int = 0) -> Sequence[_Message]:
        messages = self._messages.get(session_id, [])
        return [m for m in messages if m.seq > after_seq]

    # --- 工具调用与观察 ---

    async def set_tool_status(self, call_id: str, status: ToolCallStatus) -> _ToolCall:
        row = self._calls.get(call_id)
        if row is None:
            raise NotFoundError(f"tool call {call_id} not found")
        if status in (
            ToolCallStatus.DISPATCHED,
            ToolCallStatus.APPROVED,
            ToolCallStatus.AWAITING_APPROVAL,
        ):
            raise StateError(
                f"{call_id}: {status} must not be set via set_tool_status "
                "(awaiting comes from record_approval_request, approved/dispatched from claim)"
            )
        current = ToolCallStatus(row.status)
        if status not in TOOL_TRANSITIONS[current]:
            raise StateError(f"illegal transition {current} -> {status} for {call_id}")
        row.status = status.value
        return row

    async def claim_tool_call(
        self,
        call_id: str,
        *,
        from_status: ToolCallStatus,
        to_status: ToolCallStatus,
    ) -> bool:
        row = self._calls.get(call_id)
        if row is None:
            raise NotFoundError(f"tool call {call_id} not found")
        if to_status not in _CLAIM_TARGETS:
            raise StateError(f"{call_id}: claim target {to_status} is not allowed")
        if to_status not in TOOL_TRANSITIONS[from_status]:
            raise StateError(f"{call_id}: illegal transition {from_status} -> {to_status}")
        session = self._sessions.get(row.session_id)
        if session is None:
            raise NotFoundError(f"session {row.session_id} not found")
        if session.status in {status.value for status in TERMINAL_SESSION_STATUSES}:
            raise ConflictError(
                f"session {row.session_id} is {session.status}; cannot claim {call_id}"
            )
        if to_status == ToolCallStatus.APPROVED:
            approval = self._approval_for_call(call_id)
            if approval is None or approval.status != ApprovalStatus.APPROVED.value:
                raise ConflictError(
                    f"{call_id}: approval must be decided as approved before consuming it"
                )
        if row.status != from_status.value:
            return False
        row.status = to_status.value
        return True

    async def finish_tool_call(self, call_id: str, result: ToolResult) -> _ToolCall:
        row = self._calls.get(call_id)
        if row is None:
            raise NotFoundError(f"tool call {call_id} not found")
        session = self._sessions.get(row.session_id)
        if session is not None and session.status in {
            status.value for status in TERMINAL_SESSION_STATUSES
        }:
            raise ConflictError(
                f"session {row.session_id} is {session.status}; late tool result for {call_id} refused"
            )
        target = {
            "success": ToolCallStatus.COMPLETED,
            "error": ToolCallStatus.FAILED,
            "denied": ToolCallStatus.DENIED,
        }[result.status]
        current = ToolCallStatus(row.status)
        if target not in TOOL_TRANSITIONS[current]:
            raise StateError(f"tool call {call_id} cannot finish as {target} from {current}")
        payload = _observation_payload(result)
        await self.append_message(
            row.session_id, MessageRole.TOOL, payload, tool_call_id=call_id
        )
        row.status = target.value
        row.reversible = bool(result.receipt)
        self._observations[call_id] = payload
        return row

    async def list_tool_calls(self, session_id: str) -> Sequence[_ToolCall]:
        return [c for c in self._calls.values() if c.session_id == session_id]

    async def get_receipt(self, call_id: str) -> str | None:
        payload = self._observations.get(call_id)
        if payload is None:
            return None
        return json.loads(payload).get("receipt")

    # --- 审批记录 ---

    async def record_approval_request(
        self,
        session_id: str,
        call_id: str,
        tool_name: str,
        arguments: str | None,
        description: str | None,
    ) -> _Approval:
        session = self._sessions.get(session_id)
        if session is None:
            raise NotFoundError(f"session {session_id} not found")
        if session.status in {status.value for status in TERMINAL_SESSION_STATUSES}:
            raise ConflictError(
                f"session {session_id} is {session.status}; cannot request approval"
            )
        existing = self._approval_for_call(call_id)
        if existing is not None:
            return existing
        row = self._calls.get(call_id)
        if row is None:
            raise NotFoundError(f"tool call {call_id} not found")
        if row.session_id != session_id:
            raise StateError(f"tool call {call_id} does not belong to session {session_id}")
        if row.tool_name != tool_name or row.arguments != arguments:
            raise StateError(
                f"approval payload does not match tool call {call_id} "
                "(tool_name/arguments must be copied from the call row)"
            )
        if row.status != ToolCallStatus.PENDING.value:
            raise StateError(f"tool call {call_id} is {row.status}, cannot request approval")
        approval = _Approval(
            id=uuid4().hex,
            session_id=session_id,
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            description=description,
            status=ApprovalStatus.PENDING.value,
            decided_by=None,
            created_at=_now(),
            decided_at=None,
        )
        self._approvals[approval.id] = approval
        row.status = ToolCallStatus.AWAITING_APPROVAL.value
        return approval

    async def decide_approval(
        self, approval_id: str, decision: ApprovalDecision, *, actor: str
    ) -> _Approval:
        approval = self._approvals.get(approval_id)
        if approval is None:
            raise NotFoundError(f"approval {approval_id} not found")
        if approval.status != ApprovalStatus.PENDING.value:
            raise ApprovalError(
                f"approval {approval_id} already decided as {approval.status}"
            )
        session = self._sessions.get(approval.session_id)
        if session is not None and session.status in {
            status.value for status in TERMINAL_SESSION_STATUSES
        }:
            raise ConflictError(
                f"session {approval.session_id} is {session.status}; cannot decide approval"
            )
        call = self._calls.get(approval.call_id)
        if call is None:
            raise NotFoundError(f"tool call {approval.call_id} not found")
        if call.status != ToolCallStatus.AWAITING_APPROVAL.value:
            raise ConflictError(
                f"tool call {approval.call_id} is {call.status}, approval can no longer be decided"
            )
        approval.status = ApprovalStatus(decision).value
        approval.decided_by = actor
        approval.decided_at = _now()
        self._audits.append(
            _Audit(
                id=uuid4().hex,
                session_id=approval.session_id,
                actor=actor,
                action="approval_decided",
                detail=json.dumps(
                    {
                        "approval_id": approval.id,
                        "call_id": approval.call_id,
                        "decision": approval.status,
                    },
                    ensure_ascii=False,
                ),
                created_at=_now(),
            )
        )
        return approval

    async def get_approval(self, approval_id: str) -> _Approval | None:
        return self._approvals.get(approval_id)

    async def get_approval_for_call(self, call_id: str) -> _Approval | None:
        return self._approval_for_call(call_id)

    async def list_pending_approvals(
        self, session_id: str | None = None
    ) -> Sequence[_Approval]:
        return [
            approval
            for approval in self._approvals.values()
            if approval.status == ApprovalStatus.PENDING.value
            and (session_id is None or approval.session_id == session_id)
        ]

    def _approval_for_call(self, call_id: str) -> _Approval | None:
        for approval in self._approvals.values():
            if approval.call_id == call_id:
                return approval
        return None

    # --- 审计 ---

    async def record_audit(
        self,
        *,
        action: str,
        actor: str,
        session_id: str | None = None,
        detail: str | None = None,
    ) -> _Audit:
        row = _Audit(
            id=uuid4().hex,
            session_id=session_id,
            actor=actor,
            action=action,
            detail=detail,
            created_at=_now(),
        )
        self._audits.append(row)
        return row

    async def query_audits(
        self, *, session_id: str | None = None, limit: int = 100
    ) -> Sequence[_Audit]:
        rows = [
            row
            for row in self._audits
            if session_id is None or row.session_id == session_id
        ]
        return rows[-limit:]

    async def latest_message_seq(self, session_id: str) -> int:
        messages = self._messages.get(session_id, [])
        return messages[-1].seq if messages else 0

    # --- 一轮 ---

    def _current_turn_calls(self, session_id: str) -> list[_ToolCall]:
        """只取本轮（最后一条 user 消息之后）产生的调用。"""
        messages = self._messages.get(session_id, [])
        last_user_seq = max(
            (m.seq for m in messages if m.role == MessageRole.USER), default=0
        )
        seq_by_message = {m.id: m.seq for m in messages}
        return [
            call
            for call in self._calls.values()
            if call.session_id == session_id
            and (call.message_id is None or seq_by_message.get(call.message_id, 0) > last_user_seq)
        ]

    def _tail_role(self, session_id: str) -> str | None:
        messages = self._messages.get(session_id, [])
        for message in reversed(messages):
            if message.role not in {role.value for role in _NON_TAIL_ROLES}:
                return message.role
        return None

    def _project(self, session_id: str, *, destructive: bool) -> TurnState | None:
        session = self._sessions.get(session_id)
        if session is None:
            raise NotFoundError(f"session {session_id} not found")
        if session.status in {status.value for status in TERMINAL_SESSION_STATUSES}:
            return None

        messages = self._messages.get(session_id, [])
        iteration = _iteration(messages)
        turn_calls = self._current_turn_calls(session_id)
        in_flight = [
            call.id
            for call in turn_calls
            if call.status == ToolCallStatus.DISPATCHED.value
            and call.id not in self._observations
        ]

        if destructive and in_flight:
            for call_id in in_flight:
                self._calls[call_id].status = ToolCallStatus.UNKNOWN.value
            session.status = SessionStatus.FAILED.value
            session.updated_at = _now()
            return TurnState(
                session_id=session_id,
                status=TurnStatus.FAILED,
                iteration=iteration,
                notes={"unknown_calls": in_flight},
            )

        open_calls = [c for c in turn_calls if c.status in {s.value for s in OPEN_TOOL_STATUSES}]
        open_call_ids = tuple(call.id for call in open_calls)
        if any(c.status == ToolCallStatus.DENIED.value for c in turn_calls):
            return None
        awaiting = [c for c in open_calls if c.status == ToolCallStatus.AWAITING_APPROVAL.value]
        if len(awaiting) > 1:
            raise StateError(
                "more than one call is awaiting approval in a single turn: "
                + ", ".join(call.id for call in awaiting)
            )
        if awaiting:
            call = awaiting[0]
            approval = self._approval_for_call(call.id)
            decided = approval is not None and approval.status != ApprovalStatus.PENDING.value
            return TurnState(
                session_id=session_id,
                status=TurnStatus.WAITING_APPROVAL,
                iteration=iteration,
                pending_call_id=call.id,
                pending_approval_id=approval.id if decided else None,
                open_call_ids=open_call_ids,
            )
        if open_calls:
            state = TurnState(
                session_id=session_id,
                status=TurnStatus.OPEN,
                iteration=iteration,
                open_call_ids=open_call_ids,
            )
            if in_flight:
                state.notes["in_flight"] = list(in_flight)
            return state
        tail = self._tail_role(session_id)
        if tail in {role.value for role in _REOPENING_ROLES}:
            return TurnState(session_id=session_id, status=TurnStatus.OPEN, iteration=iteration)
        return None

    async def load_open_turn(self, session_id: str) -> TurnState | None:
        return self._project(session_id, destructive=True)

    async def peek_open_turn(self, session_id: str) -> TurnState | None:
        return self._project(session_id, destructive=False)

    # --- 事务 ---

    @asynccontextmanager
    async def transaction(self):
        snapshot = (
            copy.deepcopy(self._sessions),
            copy.deepcopy(self._messages),
            copy.deepcopy(self._calls),
            dict(self._observations),
            copy.deepcopy(self._approvals),
            [copy.deepcopy(row) for row in self._audits],
        )
        try:
            yield None
        except BaseException:
            (
                self._sessions,
                self._messages,
                self._calls,
                self._observations,
                self._approvals,
                self._audits,
            ) = snapshot
            raise
