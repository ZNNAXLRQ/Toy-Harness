"""``AgentLoop`` 的默认实现：会话级单写者 + 引擎 + 放弃语义。

为什么还要这一层（而不是直接暴露引擎）：

- **同一会话同一时刻只允许一个 turn**：用会话级 ``asyncio.Lock`` 保证，不做全局锁；
- **放弃一轮**是编排动作（过期审批 + 置终态 + 审计 + 事件），不属于「模型↔工具」引擎；
- Web 层只依赖 ``AgentLoop`` 端口，将来换实现不需要改 controller。
"""

import asyncio

from app.common.api.agent_loop import TurnOutcome
from app.common.api.audit_service import AuditService
from app.common.api.state_store import (
    TERMINAL_SESSION_STATUSES,
    SessionStatus,
    StateStore,
    TurnStatus,
)
from app.common.event.event_type import EventType
from app.common.exception import ConflictError, NotFoundError
from app.core.loop.agent_loop_engine import TurnEngine

_TERMINAL = {status.value for status in TERMINAL_SESSION_STATUSES}
_ABANDONABLE = {
    SessionStatus.IDLE.value,
    SessionStatus.OPEN.value,
    SessionStatus.WAITING_APPROVAL.value,
}


class DefaultAgentLoop:
    def __init__(
        self,
        engine: TurnEngine,
        *,
        state: StateStore,
        audit: AuditService | None = None,
    ) -> None:
        self.engine = engine
        self.state = state
        self.audit = audit
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def run_turn(
        self, session_id: str, *, user_content: str | None = None
    ) -> TurnOutcome:
        async with self._lock_for(session_id):
            return await self.engine.run(session_id, user_content=user_content)

    async def resume_turn(
        self, session_id: str, *, approval_id: str | None = None
    ) -> TurnOutcome:
        async with self._lock_for(session_id):
            return await self.engine.resume(session_id, approval_id=approval_id)

    async def abandon_turn(
        self, session_id: str, *, reason: str | None = None
    ) -> TurnOutcome:
        async with self._lock_for(session_id):
            session = await self.state.load_session(session_id)
            if session is None:
                raise NotFoundError(f"session {session_id} not found")
            if session.status not in _ABANDONABLE:
                # completed/failed/abandoned 都不该再被放弃：前者已经收尾，
                # 后两者是硬终态（update_session 会拒绝）。
                raise ConflictError(
                    f"session {session_id} is {session.status}; nothing to abandon"
                )
            # 放弃之后审批没有执行机会：先过期（置 denied + 审计），再置会话终态。
            await self.state.expire_pending_approvals(
                session_id, actor="system:abandon"
            )
            await self.state.update_session(
                session_id, status=SessionStatus.ABANDONED.value
            )
            if self.audit is not None:
                await self.audit.log(
                    action="turn_abandoned",
                    actor="system",
                    session_id=session_id,
                    detail={"reason": reason or ""},
                )
            await self.engine.publish(
                EventType.TURN_FAILED,
                session_id,
                error="Turn abandoned by user",
                unknown_calls=[],
            )
            return TurnOutcome(
                session_id=session_id,
                status=TurnStatus.ABANDONED,
                error=reason or "Turn abandoned by user",
            )
