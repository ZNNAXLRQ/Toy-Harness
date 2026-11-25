"""审计端口：把「谁、什么时候、对什么、做了什么决定」写进 audit_logs。

审计与事件不是一回事：事件是给人看的实时流（可丢、可重建），审计是不可变的
决定记录（审批、回滚、会话创建、子代理启停）。

## 存储

审计写入与查询**由 ``StateStore`` 提供**（``record_audit`` / ``query_audits``），
``AuditService`` 的实现只做委托。原因与审批相同：B 不该自己连数据库，也不该为了
「决策+审计同事务」去和 C 协商工作单元。

审批决策的审计由 ``StateStore.decide_approval`` 在同一事务里顺带写入
（``action='approval_decided'``），**``AuditService.log`` 不要重复记录这一条**，
否则审计表会出现两条矛盾的决策记录。

## 脱敏顺序（写错会泄漏密钥，务必按这个顺序）

1. 对 ``detail`` 的**值**做 ``app.common.security.redact_values``（保留键名，含嵌套）；
2. 再 ``json.dumps`` 序列化；
3. 最后交给 ``StateStore.record_audit``。

**不能反过来**：先序列化再脱敏时，密钥里的 ``"`` / ``\\`` / 换行会被 JSON 转义，
原值在序列化结果里不再出现，替换就匹配不到，密钥会原样落库。
（参考实现：``tests/fakes/memory_audit_service.py``，B 的实现照它写即可。）

字符串形式的 ``detail`` 直接过一遍 ``redact`` 即可。
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
        """写一条审计：**先对 Mapping 的值脱敏 → JSON 序列化 → 委托
        ``StateStore.record_audit``**（顺序见模块说明，反了会漏脱敏）。

        失败必须抛异常，不允许静默吞掉。
        """
        ...

    async def query(
        self, *, session_id: str | None = None, limit: int = 100
    ) -> Sequence[AuditView]:
        """委托 ``StateStore.query_audits``（最新 limit 条、created_at 升序、limit>=1）。"""
        ...
