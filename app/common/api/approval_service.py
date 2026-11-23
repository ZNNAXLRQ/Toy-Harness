"""审批端口：决策落库，turn 挂起等恢复。

**不阻塞**是本端口最重要的性质。原型的审批是同步 ``bool``：协程一等就是分钟级，
进程重启就丢；而且「拒绝」只能作为工具结果回传，无法作为执行前的持久化证据。

正确形状：

1. 敏感调用先把 ``tool_calls.status`` 置 ``awaiting_approval``，``request()``
   生成一条待审批记录并落库；
2. 驱动层**返回**，turn 进入 ``waiting_approval``（可跨进程重启）；
3. Web 层 ``decide()`` 写入决策 + 审计，然后调 ``AgentLoop.resume_turn()``。

因此 ``decide`` 只负责记录，不负责执行——执行永远由 loop 恢复时统一发起。
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence

from app.common.api.state_store import ToolCallView


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"


@dataclass(frozen=True)
class ApprovalRequest:
    """一条待审批记录（落库后由 id 标识）。"""

    id: str
    session_id: str
    call_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    description: str
    created_at: int
    decision: ApprovalDecision | None = None
    decided_by: str | None = None


class ApprovalService(Protocol):
    async def request(self, call: ToolCallView) -> ApprovalRequest:
        """为一次敏感调用创建待审批记录；重复创建返回同一条（幂等）。"""
        ...

    async def decide(
        self, approval_id: str, decision: ApprovalDecision, *, actor: str
    ) -> ApprovalRequest:
        """写入决策。已决策过的记录再次提交必须抛 ``ApprovalError``。"""
        ...

    async def pending(self, session_id: str | None = None) -> Sequence[ApprovalRequest]:
        """列出待审批；Web 用它在页面刷新后还原审批队列。"""
        ...

    async def get(self, approval_id: str) -> ApprovalRequest:
        """取单条；不存在抛 ``NotFoundError``。"""
        ...
