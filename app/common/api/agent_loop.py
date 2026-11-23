"""Agent 主循环端口：一轮对话的驱动与恢复。

**为什么是「挂起/恢复」而不是阻塞等待审批**：审批可能要等几分钟甚至跨进程重启。
把等待做成阻塞协程，重启后这一轮就消失了；做成「落库 + 返回 + 事后恢复」，则
页面刷新、服务重启、审批迟到都能继续。

不变式（本轮评审补充了后三条，缺失会造成重复派发或错配审批）：

- 同一会话同一时刻只允许一个 turn 在跑（``ConflictError``）；
- ``run_turn`` 的返回要么是 ``completed``/``failed``，要么是 ``waiting_approval``
  （此时必须带 ``pending_call_id``），并同步把 ``sessions.status`` 写成对应值；
- ``resume_turn`` 必须**显式绑定审批**：只有 ``approval_id`` 对应的待审批记录属于
  当前 ``pending_call_id``、且会话仍处于 ``waiting_approval`` 时才继续，否则
  ``ConflictError``/``ApprovalError``；
- 恢复后的派发必须先 ``claim_tool_call(...)`` 条件迁移到 ``dispatched``，**只有拿到
  ``True`` 的一方可以执行工具**——并发恢复不会重复执行；
- 恢复时遇到 ``dispatched`` 但无观察结果的调用，判 ``unknown`` 并交给人工，不重放；
- 派发时按 ``call.tool_name`` 从注册表取实现，注册表禁止同名覆盖，因此批准时看到的
  工具与执行时拿到的是同一个实现。
"""

from dataclasses import dataclass
from typing import Protocol

from app.common.api.state_store import TurnStatus


@dataclass(frozen=True)
class TurnOutcome:
    session_id: str
    status: TurnStatus
    content: str | None = None
    pending_call_id: str | None = None
    iterations: int = 0
    tokens: int = 0
    """本进程内这一轮累计的 usage；不跨重启（schema 无列，见 state_store 说明）。"""
    error: str | None = None

    @property
    def finished(self) -> bool:
        return self.status not in {TurnStatus.OPEN, TurnStatus.WAITING_APPROVAL}


class AgentLoop(Protocol):
    async def run_turn(
        self, session_id: str, *, user_content: str | None = None
    ) -> TurnOutcome:
        """执行（或继续）一轮对话，直到完成或需要审批。

        ``user_content`` 为 ``None`` 时是**恢复**语义：接上会话里已存在的未完成轮
        （例如进程崩溃后重启），而不是新起一轮。
        """
        ...

    async def resume_turn(self, session_id: str, *, approval_id: str) -> TurnOutcome:
        """消费一条已落库的审批决定并继续。

        必须校验：``approval_id`` 存在、其 ``call_id`` 等于当前 ``pending_call_id``、
        会话仍为 ``waiting_approval``、该 call 仍是 ``awaiting_approval``（未被执行）。
        随后按 ``TOOL_TRANSITIONS`` 把调用迁移到 ``dispatched`` 再执行；被拒绝则写
        ``denied`` 观察结果并结束本轮。
        """
        ...
