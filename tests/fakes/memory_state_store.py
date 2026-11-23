"""StateStore 端口的内存实现，供契约测试使用。

只在 ``tests/`` 下存在，**不进入 app 包**：应用里出现第二个状态实现就会出现两份真相。

它与将来 C 的 SQLite 实现必须通过同一套 ``tests/contracts`` 用例，包括：事务回滚、
条件派发、审批拒绝写入、UNKNOWN 的幂等判定、补偿凭据读回。
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
    TERMINAL_SESSION_STATUSES,
    TOOL_TRANSITIONS,
    AssistantTurn,
    MessageRole,
    SessionStatus,
    ToolCallStatus,
    TurnState,
    TurnStatus,
)
from app.common.api.tool import ToolResult
from app.common.exception import NotFoundError, StateError

_OPEN_TOOL_STATUSES = frozenset(
    {
        ToolCallStatus.PENDING,
        ToolCallStatus.AWAITING_APPROVAL,
        ToolCallStatus.APPROVED,
        ToolCallStatus.DISPATCHED,
    }
)
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


class MemoryStateStore:
    """满足 ``app.common.api.state_store.StateStore`` 的最小实现。"""

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._messages: dict[str, list[_Message]] = {}
        self._calls: dict[str, _ToolCall] = {}
        self._observations: dict[str, str] = {}

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
        for key, value in fields.items():
            if not hasattr(session, key):
                raise StateError(f"unknown session field: {key}")
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
        if session_id not in self._sessions:
            raise NotFoundError(f"session {session_id} not found")
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
        if session_id not in self._sessions:
            raise NotFoundError(f"session {session_id} not found")
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

    async def set_tool_status(
        self, call_id: str, status: ToolCallStatus, *, error: str | None = None
    ) -> _ToolCall:
        row = self._calls.get(call_id)
        if row is None:
            raise NotFoundError(f"tool call {call_id} not found")
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
        if to_status not in TOOL_TRANSITIONS[from_status]:
            raise StateError(f"illegal transition {from_status} -> {to_status} for {call_id}")
        if row.status != from_status.value:
            return False
        row.status = to_status.value
        return True

    async def finish_tool_call(self, call_id: str, result: ToolResult) -> _ToolCall:
        row = self._calls.get(call_id)
        if row is None:
            raise NotFoundError(f"tool call {call_id} not found")
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

    # --- 一轮 ---

    async def load_open_turn(self, session_id: str) -> TurnState | None:
        session = self._sessions.get(session_id)
        if session is None:
            raise NotFoundError(f"session {session_id} not found")
        if session.status in {status.value for status in TERMINAL_SESSION_STATUSES}:
            return None

        messages = self._messages.get(session_id, [])
        calls = [c for c in self._calls.values() if c.session_id == session_id]
        iteration = _iteration(messages)

        open_calls = [c for c in calls if c.status in {s.value for s in _OPEN_TOOL_STATUSES}]
        unknown: list[str] = []
        for call in open_calls:
            if (
                call.status == ToolCallStatus.DISPATCHED.value
                and call.id not in self._observations
            ):
                call.status = ToolCallStatus.UNKNOWN.value
                unknown.append(call.id)
        if unknown:
            # 唯一的恢复写：把这一轮钉死为 failed，避免下次读取重新打开（等于重放）。
            session.status = SessionStatus.FAILED.value
            session.updated_at = _now()
            return TurnState(
                session_id=session_id,
                status=TurnStatus.FAILED,
                iteration=iteration,
                notes={"unknown_calls": unknown},
            )

        still_open = [c for c in open_calls if c.status in {s.value for s in _OPEN_TOOL_STATUSES}]
        awaiting = [c for c in still_open if c.status == ToolCallStatus.AWAITING_APPROVAL.value]
        if awaiting:
            return TurnState(
                session_id=session_id,
                status=TurnStatus.WAITING_APPROVAL,
                iteration=iteration,
                pending_call_id=awaiting[0].id,
            )
        if still_open:
            return TurnState(session_id=session_id, status=TurnStatus.OPEN, iteration=iteration)
        if messages and messages[-1].role in {role.value for role in _REOPENING_ROLES}:
            return TurnState(session_id=session_id, status=TurnStatus.OPEN, iteration=iteration)
        return None

    # --- 事务 ---

    @asynccontextmanager
    async def transaction(self):
        snapshot = (
            copy.deepcopy(self._sessions),
            copy.deepcopy(self._messages),
            copy.deepcopy(self._calls),
            dict(self._observations),
        )
        try:
            yield None
        except BaseException:
            self._sessions, self._messages, self._calls, self._observations = snapshot
            raise
