"""Agent 主循环端口：一轮对话的驱动与恢复。

**为什么是「挂起/恢复」而不是阻塞等待审批**：审批可能要等几分钟甚至跨进程重启。
把等待做成阻塞协程，重启后这一轮就消失了；做成「落库 + 返回 + 事后恢复」，则
页面刷新、服务重启、审批迟到都能继续。

不变式：

- 同一会话同一时刻只允许一个 turn 在跑（``ConflictError``）；
- ``run_turn`` 正常返回的 ``TurnOutcome`` 要么是 ``completed``/``failed``，
  要么是 ``waiting_approval``（此时必须带 ``pending_call_id``）；
- ``resume_turn`` 只能从 ``waiting_approval`` 起步，否则 ``ConflictError``；
- 恢复时遇到 ``dispatched`` 但无观察结果的调用，判 ``unknown`` 并交给人工。
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
    error: str | None = None

    @property
    def finished(self) -> bool:
        return self.status not in {TurnStatus.OPEN, TurnStatus.WAITING_APPROVAL}


class AgentLoop(Protocol):
    async def run_turn(
        self, session_id: str, *, user_content: str | None = None
    ) -> TurnOutcome:
        """执行（或继续）一轮对话，直到完成或需要审批。"""
        ...

    async def resume_turn(self, session_id: str) -> TurnOutcome:
        """审批落库后继续；没有挂起的 turn 时抛 ``ConflictError``。"""
        ...
