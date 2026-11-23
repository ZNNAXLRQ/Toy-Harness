"""AuditService 的参考实现：只做「值脱敏 → 序列化 → 委托 StateStore」。

放在 tests/ 下是因为它的归属是 B（``app/tools/audit/``）；A 用它锁住审计契约，
B 的实现照着这个行为写即可。
"""

import json
from typing import Any, Mapping, Sequence

from app.common.security import redact, redact_values


class MemoryAuditService:
    def __init__(self, store) -> None:
        self.store = store

    async def log(
        self,
        *,
        action: str,
        actor: str,
        session_id: str | None = None,
        detail: Mapping[str, Any] | str | None = None,
    ):
        # 顺序很重要：先脱敏值、再 json.dumps。反过来的话密钥里的引号/反斜杠会被
        # JSON 转义，原值在序列化结果里不再出现，redact 就替换不到。
        if detail is None:
            text: str | None = None
        elif isinstance(detail, Mapping):
            text = json.dumps(redact_values(dict(detail)), ensure_ascii=False)
        else:
            text = redact(str(detail))
        return await self.store.record_audit(
            action=action, actor=actor, session_id=session_id, detail=text
        )

    async def query(
        self, *, session_id: str | None = None, limit: int = 100
    ) -> Sequence[Any]:
        return await self.store.query_audits(session_id=session_id, limit=limit)
