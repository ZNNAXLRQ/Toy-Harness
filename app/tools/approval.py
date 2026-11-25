# 来源：D:/Programs/Python/OOAD/Assignment1/support/runtime.py:confirm_action（17–37 行）。
# 功能：保留显式批准和默认拒绝，将终端输入移到可注入适配器，不让工具直接 input。
# 终端适配器供原型使用；正式服务通过 StateStore 记录待审批与决策。
from typing import Any, Mapping, Sequence

from app.common.api.state_store import ApprovalDecision, ApprovalView, StateStore, ToolCallView
from app.common.security import redact


class DenyApproval:
    def approve(self, request):
        return False


class TerminalApproval:
    def approve(self, request):
        print(redact(f"[APPROVAL] {request.call_id} {request.description}"), flush=True)
        try:
            answer = input("Allow this operation? [y/N]: ")
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in {"y", "yes"}


class DefaultApprovalService:
    def __init__(self, store: StateStore):
        self.store = store

    def requires_approval(self, tool_name: str, arguments: Mapping[str, Any]) -> bool:
        return tool_name not in {"read_file", "list_files"}

    async def request(self, call: ToolCallView) -> ApprovalView:
        return await self.store.record_approval_request(
            call.session_id, call.id, call.tool_name, call.arguments,
            f"Approve {call.tool_name}",
        )

    async def decide(self, approval_id: str, decision: ApprovalDecision, *, actor: str) -> ApprovalView:
        return await self.store.decide_approval(approval_id, decision, actor=actor)

    async def pending(self, session_id: str | None = None) -> Sequence[ApprovalView]:
        return await self.store.list_pending_approvals(session_id)

    async def find_for_call(self, call_id: str) -> ApprovalView | None:
        return await self.store.get_approval_for_call(call_id)
