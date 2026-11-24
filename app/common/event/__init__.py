"""事件定义与发布端口。

事件类型是 SSE 的传输契约（``event:``），发布端口是 D 的实现要求。
真相在数据库表：事件可以丢，客户端断线后走 REST 重建。

过渡说明：``runtime.py`` 里的 ``MemoryEventSink`` 是原型的注入点，
   仅在 PR2 剪切前供旧代码使用。
"""

from app.common.event.agent_event import AgentEvent
from app.common.event.event_publisher import EventPublisher
from app.common.event.event_type import EventType

__all__ = ["AgentEvent", "EventPublisher", "EventType"]
