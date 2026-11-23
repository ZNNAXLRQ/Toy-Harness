"""审批端口：判定策略 + 决策编排。持久化由状态层负责（见下）。

## 存储与原子性（本版明确）

审批记录落在 ``schema.sql`` 新增的 ``approvals`` 表（A 的偏离决策：原 7 张表没有
任何一张能表达「谁在等审批、谁批的、何时批的」）。为了让「挂起」和「决策」天然原子，
**写入一律由 ``StateStore`` 完成**，``ApprovalService`` 只做策略与委托：

| 动作 | 由谁写 | 必须同事务的内容 |
|---|---|---|
| 挂起 | ``StateStore.record_approval_request`` | 插审批行 + 把 call 置 ``awaiting_approval`` |
| 决策 | ``StateStore.decide_approval`` | 更新审批行 + 写 ``audit_logs(action='approval_decided')`` |
| 恢复 | ``StateStore.get_approval_for_call`` | —— 读，按 call 找审批（含已决策），重启后据此恢复 |

这样 B 的 ``DefaultApprovalService`` 只剩两件小事：``requires_approval`` 的策略，
以及把 request/decide 委托给 store。**B 不需要自己开事务，也不需要与 C 协商工作单元。**

## 责任划分

1. ``requires_approval`` 判定这次调用要不要人工确认（纯函数，不写库）；
2. 需要时由 loop 调 ``request()``：store 同事务落审批行并把调用置 ``awaiting_approval``；
   loop 随后**返回**，turn 进入 ``waiting_approval``（可跨进程重启）；
3. Web 层 ``decide()``：store 同事务写决策 + 审计；
4. ``AgentLoop.resume_turn()`` 消费决策：先用 ``claim_tool_call(awaiting→approved)``
   抢占消费权，再用 ``claim_tool_call(approved→dispatched)`` 抢占派发权，只有抢到
   派发权的一方执行工具；被拒绝则写 ``denied`` 观察结果并结束本轮。
5. 重启后 ``load_open_turn`` 会带出 ``pending_call_id`` + ``pending_approval_id``，
   loop 用 ``get_approval_for_call`` 判断决策是否已存在，从而决定"继续等"还是"恢复执行"。
"""

from typing import Any, Mapping, Protocol, Sequence

from app.common.api.state_store import ApprovalDecision, ApprovalView, ToolCallView

__all__ = ["ApprovalDecision", "ApprovalService", "ApprovalView"]


class ApprovalService(Protocol):
    def requires_approval(self, tool_name: str, arguments: Mapping[str, Any]) -> bool:
        """纯判定：这次调用是否需要人工确认。不写库、不弹窗、不阻塞。

        策略来自工作区/工具白名单/参数内容（例如写入工作区外的路径需要确认），
        实现见 ``app/tools/approval/permission_policy.py``（B）。
        """
        ...

    async def request(self, call: ToolCallView) -> ApprovalView:
        """创建待审批记录。

        实现必须是**一次委托**：``StateStore.record_approval_request(...)``——由 store
        在同一事务里写审批行并把调用置 ``awaiting_approval``。重复 request 返回同一条
        （幂等，按 ``call_id`` 唯一）。
        """
        ...

    async def decide(
        self, approval_id: str, decision: ApprovalDecision, *, actor: str
    ) -> ApprovalView:
        """写入决策。已决策的记录再次提交必须抛 ``ApprovalError``。

        实现是一次委托：``StateStore.decide_approval(...)``——决策与审计同事务。
        本方法**不派发工具**，派发由 loop 在 ``resume_turn`` 里抢占完成。
        """
        ...

    async def pending(self, session_id: str | None = None) -> Sequence[ApprovalView]:
        """未决策的审批队列；Web 用它渲染审批列表。"""
        ...

    async def find_for_call(self, call_id: str) -> ApprovalView | None:
        """按调用找审批记录（含已决策）。恢复时用它判断"已决策未消费"。"""
        ...
