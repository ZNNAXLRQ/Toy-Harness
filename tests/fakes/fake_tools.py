"""工具注册表与审批策略的替身（实现端口，不依赖 app/tools 的实现）。"""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence

from app.common.api.state_store import ToolCallView
from app.common.api.tool import ToolResult, ToolSpec
from app.common.exception import ToolError

Handler = Callable[[Mapping[str, Any]], "Awaitable[ToolResult] | ToolResult"]


@dataclass
class FakeTool:
    spec: ToolSpec
    handler: Handler


class FakeToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, FakeTool] = {}
        self.executed: list[tuple[str, dict[str, Any]]] = []

    # --- 构造 ---
    def add(
        self,
        name: str,
        handler: Handler,
        *,
        description: str = "",
        parameters: dict[str, Any] | None = None,
    ) -> None:
        self._tools[name] = FakeTool(
            spec=ToolSpec(
                name=name,
                description=description or name,
                parameters=parameters or {"type": "object", "properties": {}},
            ),
            handler=handler,
        )

    # --- ToolRegistry 端口 ---
    @property
    def schemas(self) -> Sequence[Mapping[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.spec.name,
                    "description": tool.spec.description,
                    "parameters": tool.spec.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    def get(self, name: str) -> FakeTool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool: {name}")
        return tool

    def register(self, tool: Any) -> None:
        if tool.spec.name in self._tools:
            raise ToolError(f"tool already registered: {tool.spec.name}")
        self._tools[tool.spec.name] = tool

    async def execute(
        self, call_id: str, name: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        tool = self.get(name)
        self.executed.append((name, dict(arguments)))
        try:
            result = tool.handler(arguments)
            if hasattr(result, "__await__"):
                result = await result
        except Exception as error:  # noqa: BLE001 - 端口契约：错误折叠成结果
            return ToolResult("error", f"Tool error ({type(error).__name__}): {error}")
        if isinstance(result, ToolResult):
            return result
        return ToolResult("success", result)


class FakeApprovalService:
    """按工具名判定是否需要审批；写入全部委托给 StateStore。"""

    def __init__(self, store, sensitive: Sequence[str] = ()) -> None:
        self.store = store
        self.sensitive = set(sensitive)
        self.request_calls = 0

    def requires_approval(self, tool_name: str, arguments: Mapping[str, Any]) -> bool:
        return tool_name in self.sensitive

    async def request(self, call: ToolCallView):
        self.request_calls += 1
        return await self.store.record_approval_request(
            call.session_id,
            call.id,
            call.tool_name,
            call.arguments,
            f"{call.tool_name}: needs approval",
        )

    async def decide(self, approval_id, decision, *, actor: str):
        return await self.store.decide_approval(approval_id, decision, actor=actor)

    async def pending(self, session_id: str | None = None):
        return await self.store.list_pending_approvals(session_id)

    async def find_for_call(self, call_id: str):
        return await self.store.get_approval_for_call(call_id)
