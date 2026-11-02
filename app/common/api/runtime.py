# 来源：D:/Programs/Python/OOAD/Assignment1/agent.py 的 client/tools/functions/observer 注入点（245–338 行）。
# 功能：将隐式约定提取为 Provider、工具执行、审批和事件接口，Core 不导入 Tools/State。
# 后续：与团队统一异步、会话、持久化审批及 StateStore 接口；当前只支持同步单轮运行。
from typing import Any, Protocol

from app.common.model.runtime import ApprovalRequest, ModelResponse, ToolResult


class LLMProvider(Protocol):
    def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse: ...


class ToolExecutor(Protocol):
    @property
    def schemas(self) -> list[dict]: ...

    def execute(self, call_id: str, name: str, arguments: dict[str, Any]) -> ToolResult: ...


class ApprovalService(Protocol):
    def approve(self, request: ApprovalRequest) -> bool: ...


class EventSink(Protocol):
    def publish(self, event: dict[str, Any]) -> None: ...