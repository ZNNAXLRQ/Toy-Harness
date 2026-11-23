"""端口契约的内存实现，供测试与「C 尚未交付」时使用。

它只在 ``tests/`` 下存在，**不进入 app 包**：应用里任何地方出现第二个状态实现
都会造成两份真相。
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Sequence
from uuid import uuid4

from app.common.api.llm_provider import ModelToolCall
from app.common.api.state_store import ToolCallStatus, TurnState, TurnStatus
from app.common.api.tool import ToolResult
from app.common.exception import NotFoundError, StateError

_OPEN_STATUSES = {
    ToolCallStatus.PENDING,
    ToolCallStatus.AWAITING_APPROVAL,
    ToolCallStatus.APPROVED,
    ToolCallStatus.DISPATCHED,
}


def _now() -> int:
    return int(time.time() * 1000)


def _iteration(messages: Sequence[_Message]) -> int:
    """本轮已发生的模型调用次数：最后一条 user 消息之后的 assistant 条数。"""
    start = 0
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            start = index + 1
            break
    return sum(1 for message in messages[start:] if message.role == "assistant")


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
        self._observed: set[str] = set()

    # --- 会话 ---

    async def create_session(
        self, *, title: str | None = None, agents_md_version: str | None = None
    ) -> _Session:
        now = _now()
        session = _Session(
            id=uuid4().hex,
            title=title,
            status="active",
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
        role: str,
        content: str | None,
        *,
        tool_call_id: str | None = None,
    ) -> _Message:
        if session_id not in self._sessions:
            raise NotFoundError(f"session {session_id} not found")
        messages = self._messages[session_id]
        message = _Message(
            id=uuid4().hex,
            session_id=session_id,
            seq=len(messages) + 1,
            role=role,
            content=content,
            tool_call_id=tool_call_id,
            created_at=_now(),
        )
        messages.append(message)
        return message

    async def list_messages(self, session_id: str, *, after_seq: int = 0) -> Sequence[_Message]:
        messages = self._messages.get(session_id, [])
        return [m for m in messages if m.seq > after_seq]

    # --- 工具调用 ---

    async def record_tool_calls(
        self,
        session_id: str,
        message_id: str | None,
        calls: Sequence[ModelToolCall],
    ) -> Sequence[_ToolCall]:
        recorded = []
        for call in calls:
            if call.id in self._calls:
                raise StateError(f"tool call {call.id} already recorded")
            row = _ToolCall(
                id=call.id,
                session_id=session_id,
                message_id=message_id,
                tool_name=call.name,
                arguments=call.arguments,
                status=ToolCallStatus.PENDING.value,
                reversible=None,
                created_at=_now(),
            )
            self._calls[call.id] = row
            recorded.append(row)
        return recorded

    async def set_tool_status(
        self, call_id: str, status: ToolCallStatus, *, error: str | None = None
    ) -> _ToolCall:
        row = self._calls.get(call_id)
        if row is None:
            raise NotFoundError(f"tool call {call_id} not found")
        row.status = status.value
        return row

    async def finish_tool_call(self, call_id: str, result: ToolResult) -> _ToolCall:
        row = self._calls.get(call_id)
        if row is None:
            raise NotFoundError(f"tool call {call_id} not found")
        if row.status not in {
            ToolCallStatus.DISPATCHED.value,
            ToolCallStatus.APPROVED.value,
            ToolCallStatus.PENDING.value,
        }:
            raise StateError(f"tool call {call_id} is not executable: {row.status}")
        # 先序列化再改状态：失败时状态与消息都保持原样（与 SQLite 实现的事务等价）。
        try:
            payload = json.dumps(result.to_dict(), ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise StateError(f"工具结果无法序列化，调用状态保持不变: {error}") from error
        await self.append_message(row.session_id, "tool", payload, tool_call_id=call_id)
        row.status = (
            ToolCallStatus.COMPLETED.value
            if result.status == "success"
            else ToolCallStatus.FAILED.value
            if result.status == "error"
            else ToolCallStatus.DENIED.value
        )
        self._observed.add(call_id)
        return row

    async def list_tool_calls(self, session_id: str) -> Sequence[_ToolCall]:
        return [c for c in self._calls.values() if c.session_id == session_id]

    # --- 一轮 ---

    async def load_open_turn(self, session_id: str) -> TurnState | None:
        messages = self._messages.get(session_id, [])
        calls = [c for c in self._calls.values() if c.session_id == session_id]
        if not messages and not calls:
            return None

        iteration = _iteration(messages)
        open_statuses = {status.value for status in _OPEN_STATUSES}
        open_calls = [c for c in calls if c.status in open_statuses]
        unknown: list[str] = []
        for call in open_calls:
            if call.status == ToolCallStatus.DISPATCHED.value and call.id not in self._observed:
                call.status = ToolCallStatus.UNKNOWN.value
                unknown.append(call.id)
        still_open = [c for c in open_calls if c.status in open_statuses]
        awaiting = [c for c in still_open if c.status == ToolCallStatus.AWAITING_APPROVAL.value]
        if awaiting:
            return TurnState(
                session_id=session_id,
                status=TurnStatus.WAITING_APPROVAL,
                iteration=iteration,
                pending_call_id=awaiting[0].id,
                notes={"unknown_calls": unknown},
            )
        if unknown:
            return TurnState(
                session_id=session_id,
                status=TurnStatus.FAILED,
                iteration=iteration,
                notes={"unknown_calls": unknown},
            )
        if still_open:
            return TurnState(session_id=session_id, status=TurnStatus.OPEN, iteration=iteration)
        # 没有待执行的调用：最后一条不是 assistant，说明这一轮还没收尾
        # （用户消息已落但模型未回复，或工具结果已落等待下一次模型调用）。
        if messages and messages[-1].role != "assistant":
            return TurnState(session_id=session_id, status=TurnStatus.OPEN, iteration=iteration)
        return None

    @asynccontextmanager
    async def transaction(self):
        yield None
