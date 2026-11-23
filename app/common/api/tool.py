"""工具端口：内置工具与插件的统一契约，含回滚补偿。

**同步实现、异步契约**：内置工具（读写文件、跑命令）写成普通同步函数最好写也
最好测，``app/tools/registry`` 在构造时统一用 ``asyncio.to_thread`` 包装一次。
因此本端口是 ``async``，但实现方不需要到处写 ``to_thread``。

**回滚凭据（receipt）**：写操作成功时返回一个由工具自己定义的补偿凭据
（例如备份文件路径、操作序号），``rollback(receipt)`` 据此撤销。不可回滚的
工具返回 ``receipt=None``——这比事后猜「这个操作能不能撤」可靠。

后续：插件式扩展沿用同一端口，不另起一套注册/执行协议。
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol

ToolStatus = Literal["success", "error", "denied"]


@dataclass(frozen=True)
class ToolSpec:
    """工具元数据。

    ``parameters`` 是 JSON Schema（object 根）；``description`` 直接进模型提示，
    要写成「什么时候用」而不是「参数是什么」。
    """

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """一次工具执行的结果。

    - ``success`` / ``error`` 由工具实现产生，都会作为观察结果喂回模型；
    - ``denied`` 只由 **loop** 在审批被拒时构造（作为观察结果落库），
      工具实现与注册表都不得返回它——这样才能保证「拒绝」永远带有审批记录；
    - ``receipt`` 是补偿凭据，``None`` 表示该操作不可回滚。
    """

    status: ToolStatus
    data: Any = None
    receipt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "result": self.data}


class Tool(Protocol):
    """单个工具。"""

    @property
    def spec(self) -> ToolSpec: ...

    async def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        """执行工具；参数校验失败也算 ``status="error"``，不要抛异常。"""
        ...

    async def rollback(self, receipt: str) -> ToolResult:
        """按凭据撤销一次已完成的写操作。不可回滚时抛 ``ToolError``。"""
        ...
