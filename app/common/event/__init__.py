"""事件定义与发布端口。

事件类型是 SSE 的传输契约（``event:``），发布端口是 D 的实现要求。
真相在数据库表：事件可以丢，客户端断线后走 REST 重建。

``EventContractError``：payload 不符合白名单/必填键时抛出的**代码 bug**，
不允许被 best-effort 的发布逻辑吞掉。

过渡说明：``runtime.py`` 里的 ``MemoryEventSink`` 是原型的注入点，
   仅在 B 迁移完成前供旧代码使用。
"""

from app.common.event.agent_event import AgentEvent, EventContractError
from app.common.event.event_publisher import EventPublisher
from app.common.event.event_type import EventType

__all__ = ["AgentEvent", "EventContractError", "EventPublisher", "EventType"]
