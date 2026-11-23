"""审计端口：把「谁、在什么时候、对什么、做了什么决定」写进 audit_logs。

审计与事件不是一回事：事件是给人看的实时流（可丢、可重建），审计是不可变的
决定记录（审批、回滚、会话创建、子代理启停）。**审批决策必须先写审计再恢复
turn**，否则出现「执行了但查不到谁批的」。
"""

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class AuditEntry:
    id: str
    session_id: str | None
    actor: str
    action: str
    detail: Mapping[str, Any] | None
    created_at: int


class AuditService(Protocol):
    async def log(
        self,
        *,
        action: str,
        actor: str,
        session_id: str | None = None,
        detail: Mapping[str, Any] | str | None = None,
    ) -> AuditEntry:
        """写一条审计；失败必须抛异常，不允许静默吞掉。"""
        ...

    async def query(
        self, *, session_id: str | None = None, limit: int = 100
    ) -> Sequence[AuditEntry]: ...
