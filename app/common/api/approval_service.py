"""审批端口：判定哪些调用需要人工确认，决策落库，turn 挂起等恢复。

**不阻塞**是本端口最重要的性质。原型的审批是同步 ``bool``：协程一等就是分钟级，
进程重启就丢；而且「拒绝」只能作为工具结果回传，无法作为执行前的持久化证据。

责任划分（避免「谁该记录待审批」这类含糊）：

1. ``requires_approval`` 判定这次调用要不要人工确认（纯函数，不写库）；
2. 需要时由 **loop** 落 ``tool_calls.status = awaiting_approval``，再调 ``request()``
   生成待审批记录并落库，然后**返回**，turn 进入 ``waiting_approval``；
3. Web 层 ``decide()`` 写入决策 + 审计；
4. ``AgentLoop.resume_turn()`` 消费决策并派发（批准 → 执行，拒绝 → 构造
   ``ToolResult("denied")`` 作为观察结果并停止本轮）。

``decide`` 只记录，不执行——执行永远由 loop 恢复时统一发起。
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
    def requires_approval(self, tool_name: str, arguments: Mapping[str, Any]) -> bool:
        """纯判定：这次调用是否需要人工确认。不写库、不弹窗、不阻塞。

        策略来自工作区/工具白名单/参数内容（例如写入工作区外的路径需要确认），
        实现见 ``app/tools/approval/permission_policy.py``（B）。
        """
        ...

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
