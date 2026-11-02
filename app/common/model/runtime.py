# 来源：D:/Programs/Python/OOAD/Assignment1/agent.py 的 serial_calls、finish、tool_end（263–338 行）。
# 功能：提取模型响应、工具调用、审批请求和工具结果，避免跨模块依赖具体实现。
# 后续：补充持久化 ID、UNKNOWN/待审批状态、检查点及版本；当前不是数据库领域模型。
import json
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def to_message(self):
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.arguments}}


@dataclass
class ModelResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = "stop"
    tokens: int = 0
    reasoning_content: str | None = None


@dataclass(frozen=True)
class ApprovalRequest:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    workspace: str
    description: str


@dataclass
class ToolResult:
    status: Literal["success", "error", "denied"]
    data: Any
    approved: bool | None = None

    def to_dict(self):
        result = {"status": self.status, "result": self.data}
        if self.approved is not None:
            result["approved"] = self.approved
        return result

    def to_content(self):
        return json.dumps(self.to_dict(), ensure_ascii=False)