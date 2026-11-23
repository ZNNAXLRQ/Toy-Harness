"""工具注册表端口：给模型看的 schema、给执行层用的统一入口。

**注册表只负责执行，审批不在它内部。** 审批要求「先落库、再挂起、等恢复」，
把它塞进 ``execute`` 就只有两种坏结局：要么阻塞等一个 bool（进程重启即丢失），
要么返回一个根本无法表达「等待审批」的 ``ToolResult``。因此流程由 loop 编排：

    loop:  approval.requires_approval(name, arguments)?
           ├─ 否 → registry.execute() → store.finish_tool_call()
           └─ 是 → store.set_tool_status(awaiting_approval)
                   → approval.request(call) → 返回 waiting_approval
           approve 之后由 loop 的 resume_turn 继续派发，注册表不参与决策。

两条约束：

1. ``schemas`` 的顺序在同一会话内必须稳定——工具定义参与 prefix cache。
2. ``execute`` 是唯一的执行入口，Schema 校验在这里做；loop 不得绕过它直接调用
   工具函数。
"""

from typing import Any, Mapping, Protocol, Sequence

from app.common.api.tool import Tool, ToolResult


class ToolRegistry(Protocol):
    @property
    def schemas(self) -> Sequence[Mapping[str, Any]]:
        """OpenAI 兼容的 tools 定义列表，顺序稳定。"""
        ...

    def get(self, name: str) -> Tool:
        """按名字取工具；回滚服务靠它找到补偿实现。未知名字抛 ``ToolError``。"""
        ...

    def register(self, tool: Tool) -> None:
        """注册（或覆盖）一个工具；schema 与签名不一致时抛 ``ToolError``。"""
        ...

    async def execute(
        self, call_id: str, name: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        """校验参数并执行，返回 ``success``/``error``。

        不弹审批、不返回 ``denied``（那是 loop 在审批拒绝时构造的观察结果）；
        不抛业务异常，参数非法与工具内部失败都折叠成 ``status="error"``。
        仅当注册表自身坏掉（未知工具名、schema 不一致）才抛 ``ToolError``。
        """
        ...
