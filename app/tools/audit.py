import json
from typing import Any, Mapping, Sequence

from app.common.api.state_store import AuditView, StateStore
from app.common.security import redact, redact_values


class DefaultAuditService:
    def __init__(self, store: StateStore):
        self.store = store

    async def log(
        self,
        *,
        action: str,
        actor: str,
        session_id: str | None = None,
        detail: Mapping[str, Any] | str | None = None,
    ) -> AuditView:
        if isinstance(detail, Mapping):
            # Redact before JSON escaping changes the secret's representation.
            text = json.dumps(redact_values(dict(detail)), ensure_ascii=False)
        else:
            text = redact(detail) if detail is not None else None
        return await self.store.record_audit(
            action=action, actor=actor, session_id=session_id, detail=text,
        )

    async def query(
        self, *, session_id: str | None = None, limit: int = 100,
    ) -> Sequence[AuditView]:
        return await self.store.query_audits(session_id=session_id, limit=limit)
