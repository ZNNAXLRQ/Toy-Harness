"""事件发布端口。

**best-effort**：推送失败不得让一次 turn 失败，也不构成正确性依赖。原型里
「事件投递失败就整轮失败」是把可观测性当业务依赖，这里明确反过来：先落库、再推送，
推送抛错只记日志。

## 断线重连要拿什么

「重连后拉一次 messages」**不够**：待审批记录、工具调用状态、会话状态都可能变化而
不产生任何新消息（审批在等、工具在跑、会话被放弃）。重连的权威快照必须包括：

1. ``GET /api/sessions/{id}`` —— 会话状态（idle/open/waiting_approval/failed/…）
   和 ``prefix_hash``；
2. ``GET /api/sessions/{id}/messages`` —— 消息全量（按 ``seq`` 排序）；
3. ``GET /api/sessions/{id}/tool-calls`` —— 每个调用的状态与是否可回滚；
4. ``GET /api/sessions/{id}/approvals?pending=1`` —— 待审批队列。

前端应按「会话状态 → 消息 → 工具调用 → 待审批」重建页面，再挂 SSE 接收增量。
若这四次请求之间的间隙需要更严格的一致性，D 可以加一个聚合快照端点，但**权威数据
仍然是这四类行**，事件永不作为唯一真相。

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
