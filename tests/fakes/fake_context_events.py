"""上下文管理与事件发布的替身：直接透传 / 记录事件。"""

from typing import Any, AsyncIterator, Mapping, Sequence

from app.common.api.context_manager import PreparedContext
from app.common.event.agent_event import AgentEvent
from app.common.event.event_publisher import EventPublisher


class PassThroughContext:
    """不裁剪、不摘要，只记录用量；测试需要摘要时用 ``compact_once``。"""

    def __init__(self, *, compact_after_first_call: bool = False) -> None:
        self.records: list[int] = []
        self.prepared: list[PreparedContext] = []
        self._compact_pending = compact_after_first_call
        self._calls = 0

    async def prepare(
        self,
        session_id: str,
        prefix: Sequence[Mapping[str, Any]],
        messages: Sequence[Mapping[str, Any]],
        *,
        budget: int | None = None,
    ) -> PreparedContext:
        self._calls += 1
        prepared = PreparedContext(
            prefix=prefix,
            messages=messages,
            estimated_tokens=sum(
                len(str(message.get("content") or "")) // 4 for message in messages
            ),
        )
        self.prepared.append(prepared)
        return prepared

    async def build_summary(
        self, session_id: str, messages: Sequence[Mapping[str, Any]]
    ) -> str:
        return f"summary of {len(messages)} messages"

    def record_usage(self, tokens: int) -> None:
        self.records.append(tokens)

    def should_compact(self) -> bool:
        if self._compact_pending:
            self._compact_pending = False
            return True
        return False


class RecordingEventPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[AgentEvent] = []
        self.fail = fail

    async def publish(self, event: AgentEvent) -> None:
        if self.fail:
            raise RuntimeError("event sink unavailable")
        self.events.append(event)

    def subscribe(self, session_id: str) -> AsyncIterator[AgentEvent]:
        return self._empty()

    async def _empty(self) -> AsyncIterator[AgentEvent]:
        if False:  # pragma: no cover - 只为满足 async iterator 形状
            yield AgentEvent(type="tool_start", session_id="")  # type: ignore[arg-type]

    def kinds(self) -> list[str]:
        return [str(event.type) for event in self.events]
