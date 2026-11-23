"""Agent 主循环端口：一轮对话的驱动与恢复。

**为什么是「挂起/恢复」而不是阻塞等待审批**：审批可能要等几分钟甚至跨进程重启。
把等待做成阻塞协程，重启后这一轮就消失了；做成「落库 + 返回 + 事后恢复」，则
页面刷新、服务重启、审批迟到都能继续。

不变式（本版补齐了审批绑定、消费权与只读投影）：

- 同一会话同一时刻只允许一个 turn 在跑（``ConflictError``）；实现用会话级锁保证。
- ``run_turn`` 的返回要么是 ``completed``/``failed``，要么是 ``waiting_approval``
  （此时带 ``pending_call_id`` 与 ``pending_approval_id``），并同步把
  ``sessions.status`` 写成对应值；turn 开始时也应在同一事务里写 ``OPEN``。
- ``resume_turn`` 的审批绑定规则：
  - 传了 ``approval_id``：它必须属于当前 ``pending_call_id``、会话仍是
    ``waiting_approval``、且该审批**已决策**；
  - 没传：用 ``StateStore.get_approval_for_call(pending_call_id)`` 找回
    「已决策未消费」的审批（重启后靠这条恢复）。
- 消费与派发各是一次 CAS：``claim_tool_call(awaiting→approved)`` 抢占审批消费权
  （**store 会校验审批记录必须是 approved**），``claim_tool_call(approved→dispatched)``
  抢占派发权。**只有抢到派发权的一方才执行工具**；抢不到说明别人已经在执行，
  本调用者必须放弃（返回 ``ConflictError`` 或 ``waiting``，不许重试执行）。
- 崩在两次 CAS 之间（``approved`` 态）时，恢复入口返回 ``OPEN`` 且
  ``TurnState.open_call_ids`` 里带着该调用；loop **按这些 id 逐个重新 CAS**，
  不需要自己再推导「哪些调用属于本轮」。
- 恢复时遇到 ``dispatched`` 但无观察结果的调用，判 ``unknown`` 并交给人工，不重放；
  该判定由 ``load_open_turn`` 独占执行，UI/只读查询用 ``peek_open_turn``。
- 派发时按 ``call.tool_name`` 从注册表取实现，注册表禁止同名覆盖，因此批准时看到的
  工具与执行时拿到的是同一个实现。
- 审批决策的审计由 ``StateStore.decide_approval`` 在同一事务写入；loop 不要再重复
  写 ``approval_decided``。
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
    pending_approval_id: str | None = None
    """挂起时对应的审批记录；没有审批记录（旧数据/异常）时为 ``None``。"""
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
        （例如进程崩溃后重启），而不是新起一轮。恢复时应先调 ``load_open_turn``；
        没有 open turn 时返回 ``failed``/``completed`` 语义的空结果或抛
        ``ConflictError``（实现二选一并写进文档）。
        """
        ...

    async def resume_turn(
        self, session_id: str, *, approval_id: str | None = None
    ) -> TurnOutcome:
        """消费一条已落库的审批决定并继续（规则见模块开头）。"""
        ...

    async def abandon_turn(self, session_id: str, *, reason: str | None = None) -> TurnOutcome:
        """人工放弃当前轮：把会话置 ``abandoned`` 并写审计；此后不可恢复。"""
        ...
