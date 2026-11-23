"""事件发布端口。

**best-effort**：推送失败不得让一次 turn 失败，也不构成正确性依赖。原型里
「事件投递失败就整轮失败」是把可观测性当业务依赖，这里明确反过来：

- 先落库、再推送；推送抛错只记日志；
- 客户端断线后靠 ``GET /api/sessions/{id}/messages`` 重建，不靠补发。

实现放 ``app/web/sse/sse_event_publisher.py``（D）。订阅是异步迭代器，方便
``sse-starlette`` 直接转成响应流。
"""

from typing import AsyncIterator, Protocol

from app.common.event.agent_event import AgentEvent


class EventPublisher(Protocol):
    async def publish(self, event: AgentEvent) -> None:
        """推给所有订阅者；没有订阅者时是空操作（不是错误）。"""
        ...

    def subscribe(self, session_id: str) -> AsyncIterator[AgentEvent]:
        """订阅某会话的事件流，直到客户端断开或调用方退出迭代。"""
        ...
