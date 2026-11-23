"""事件对象。

事件是**视图**，不是真相：真相在 ``messages`` / ``tool_calls`` / ``observations`` /
``approvals``。因此事件可以丢、可以重发，客户端断线后拉 REST 即可对齐。
``seq`` 取对应的 ``messages.seq``，让前端能把一条事件钉在会话时间线的正确位置。

两个强制保证（本版补上）：

1. **构造即脱敏**：``payload`` 在 ``__post_init__`` 里统一经 ``redact_value`` 处理，
   工具参数、provider 错误体里的密钥不会进 SSE；
2. **构造即校验**：payload 的键必须在 ``PAYLOAD_FIELDS[type]`` 内、必填键必须在
   ``REQUIRED_PAYLOAD_FIELDS[type]`` 内，否则 ``ValueError``——避免"每个层各塞各的"。

时间统一用 **int 毫秒**（与数据库实体一致），不要混 float 秒。
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.common.event.event_type import (
    PAYLOAD_FIELDS,
    REQUIRED_PAYLOAD_FIELDS,
    EventType,
)
from app.common.security import redact_value


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class AgentEvent:
    type: EventType
    session_id: str
    seq: int = 0
    payload: Mapping[str, Any] = field(default_factory=dict)
    created_at: int = field(default_factory=_now_ms)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", redact_value(dict(self.payload)))
        self.validate_payload()

    def validate_payload(self) -> None:
        event_type = EventType(self.type)
        allowed = PAYLOAD_FIELDS[event_type]
        required = REQUIRED_PAYLOAD_FIELDS[event_type]
        unexpected = set(self.payload) - allowed
        missing = required - set(self.payload)
        if unexpected:
            raise ValueError(
                f"{event_type}: unexpected payload keys {sorted(unexpected)} "
                f"(allowed: {sorted(allowed)})"
            )
        if missing:
            raise ValueError(f"{event_type}: missing required payload keys {sorted(missing)}")

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
