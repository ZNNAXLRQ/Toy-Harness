"""工具注册表端口：给模型看的 schema、给执行层用的统一入口。

两条约束：

1. ``schemas`` 的顺序在同一会话内必须稳定——工具定义的顺序参与 prefix cache。
2. ``execute`` 是唯一的执行入口，审批与 Schema 校验都在注册表内部完成；
   core/loop 不得绕过它直接调用工具函数。
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
        """校验参数、走审批、执行；任何失败都折叠成 ``ToolResult``。"""
        ...
