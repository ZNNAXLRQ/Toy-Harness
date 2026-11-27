"""SubagentRunStore 的内存实现（契约测试与调度器测试共用）。"""

from dataclasses import dataclass
from typing import Sequence
from uuid import uuid4

from app.common.exception import NotFoundError, StateError


def _now() -> int:
    import time

    return int(time.time() * 1000)


@dataclass
class _Run:
    id: str
    session_id: str
    parent_task_id: str | None
    status: str
    context_json: str | None
    result_json: str | None
    created_at: int


class MemorySubagentRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, _Run] = {}

    async def save_run(
        self,
        run_id: str,
        session_id: str,
        *,
        parent_task_id: str | None = None,
        status: str = "running",
        context_json: str | None = None,
    ) -> _Run:
        if run_id in self._runs:
            raise StateError(f"subagent run {run_id} already exists")
        row = _Run(
            id=run_id or uuid4().hex,
            session_id=session_id,
            parent_task_id=parent_task_id,
            status=status,
            context_json=context_json,
            result_json=None,
            created_at=_now(),
        )
        self._runs[row.id] = row
        return row

    async def update_run(
        self, run_id: str, *, status: str, result_json: str | None = None
    ) -> _Run:
        row = self._runs.get(run_id)
        if row is None:
            raise NotFoundError(f"subagent run {run_id} not found")
        row.status = status
        if result_json is not None:
            row.result_json = result_json
        return row

    async def load_run(self, run_id: str) -> _Run | None:
        return self._runs.get(run_id)

    async def list_runs(self, session_id: str) -> Sequence[_Run]:
        return [
            row
            for row in sorted(self._runs.values(), key=lambda item: item.created_at)
            if row.session_id == session_id
        ]
