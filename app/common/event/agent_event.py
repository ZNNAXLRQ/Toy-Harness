"""事件对象。

事件是**视图**，不是真相：真相在 ``messages`` / ``tool_calls`` / ``observations`` /
``approvals``。因此事件可以丢、可以重发，客户端断线后拉 REST 即可对齐。
``seq`` 取对应的 ``messages.seq``，让前端能把一条事件钉在会话时间线的正确位置。

``seq`` 的准确语义（D 必须按这个理解，不要当事件 id 用）：

- **它是"消息锚点"，不是事件唯一 id**：一条消息可能对应多个事件（例如 assistant
  消息 → ``model_response`` + ``tool_start``/``tool_end``），这些事件的 ``seq`` 相同；
- ``seq=0`` 表示**不锚定任何持久化消息**（如子代理的 ``subagent_started/finished``，
  子代理 transcript 不落主会话）；
- 事件流只反映已持久化的消息：模型响应不完整（``length``/``content_filter``）时
  不落 assistant 消息，也就不发 ``model_response``，只发 ``turn_failed``。

两个强制保证：

1. **构造即校验**：payload 的键必须在 ``PAYLOAD_FIELDS[type]`` 内、必填键必须在
   ``REQUIRED_PAYLOAD_FIELDS[type]`` 内，否则 ``ValueError``。
2. **构造即脱敏**：**先校验、后脱敏，且只脱敏值不脱敏键**。顺序反了会出现一个隐蔽
   bug——如果某个 ``*_SECRET`` 环境变量的值恰好等于 payload 键名，键会被替换成
   ``[REDACTED]``，于是校验报「unexpected payload keys」，看起来像代码错误。

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
from app.common.security import redact_values


def _now_ms() -> int:
    return int(time.time() * 1000)


class EventContractError(ValueError):
    """事件 payload 不符合契约——这是**代码 bug**，不允许被 best-effort 逻辑吞掉。"""


@dataclass(frozen=True)
class AgentEvent:
    type: EventType
    session_id: str
    seq: int = 0
    payload: Mapping[str, Any] = field(default_factory=dict)
    created_at: int = field(default_factory=_now_ms)

    def __post_init__(self) -> None:
        self.validate_payload()
        # 先校验后脱敏、且只脱敏值（含嵌套）：键是契约的一部分，不能被动过。
        object.__setattr__(self, "payload", redact_values(dict(self.payload)))

    def validate_payload(self) -> None:
        event_type = EventType(self.type)
        allowed = PAYLOAD_FIELDS[event_type]
        required = REQUIRED_PAYLOAD_FIELDS[event_type]
        unexpected = set(self.payload) - allowed
        missing = required - set(self.payload)
        if unexpected:
            raise EventContractError(
                f"{event_type}: unexpected payload keys {sorted(unexpected)} "
                f"(allowed: {sorted(allowed)})"
            )
        if missing:
            raise EventContractError(
                f"{event_type}: missing required payload keys {sorted(missing)}"
            )

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
