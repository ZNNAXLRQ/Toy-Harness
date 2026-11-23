"""事件对象。

事件是**视图**，不是真相：真相在 ``messages`` / ``tool_calls`` / ``observations``。
因此事件可以丢、可以重发，客户端断线后拉 REST 即可对齐。``seq`` 取对应的
``messages.seq``，让前端能把一条事件钉在会话时间线的正确位置。
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.common.event.event_type import EventType


@dataclass(frozen=True)
class AgentEvent:
    type: EventType
    session_id: str
    seq: int = 0
    payload: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": str(self.type),
            "session_id": self.session_id,
            "seq": self.seq,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }

    def to_sse(self) -> dict[str, str]:
        """``sse-starlette`` 的 ServerSentEvent 形状。"""
        return {"event": str(self.type), "data": json.dumps(self.to_dict(), ensure_ascii=False)}
