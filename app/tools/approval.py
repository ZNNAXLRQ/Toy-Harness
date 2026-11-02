# 来源：D:/Programs/Python/OOAD/Assignment1/support/runtime.py:confirm_action（17–37 行）。
# 功能：保留显式批准和默认拒绝，将终端输入移到可注入适配器，不让工具直接 input。
# 后续：实现 SQLite 待审批记录、Web 决策和一次性消费；当前审批仅在当前同步调用有效。
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