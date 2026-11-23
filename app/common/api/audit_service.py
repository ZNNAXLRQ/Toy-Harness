"""审计端口：把「谁、什么时候、对什么、做了什么决定」写进 audit_logs。

审计与事件不是一回事：事件是给人看的实时流（可丢、可重建），审计是不可变的
决定记录（审批、回滚、会话创建、子代理启停）。

## 存储（本版明确）

审计写入与查询**由 ``StateStore`` 提供**（``record_audit`` / ``query_audits``），
``AuditService`` 的实现只做委托。原因与审批相同：B 不该自己连数据库，也不该为了
「决策+审计同事务」去和 C 协商工作单元。

审批决策的审计由 ``StateStore.decide_approval`` 在同一事务里顺带写入
（``action='approval_decided'``），**``AuditService.log`` 不要重复记录这一条**，
否则审计表会出现两条矛盾的决策记录。

## 脱敏

``detail`` 在交给 store 之前必须经 ``app.common.security.redact`` 处理：
审计要长期保留，不能因为「反正是内部表」就把密钥写进去。``AuditService.log``
接受 Mapping 时负责序列化（JSON）并脱敏。
"""

from typing import Any, Mapping, Protocol, Sequence

from app.common.api.state_store import AuditView


class AuditService(Protocol):
    async def log(
        self,
        *,
        action: str,
        actor: str,
        session_id: str | None = None,
        detail: Mapping[str, Any] | str | None = None,
    ) -> AuditView:
        """写一条审计（序列化 + 脱敏后委托 ``StateStore.record_audit``）。

        失败必须抛异常，不允许静默吞掉。
        """
        ...

    async def query(
        self, *, session_id: str | None = None, limit: int = 100
    ) -> Sequence[AuditView]:
        """委托 ``StateStore.query_audits``。"""
        ...
