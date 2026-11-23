"""跨模块抽象接口（端口）。

规则：``common`` 不 import 任何实现层；实现层只实现这里的 Protocol。

- 端口 DTO（``ModelResponse``、``CompletionRequest``、``ApprovalRequest`` 等）放在本包，
  它们是**不落库**的传输形状。
- 落库实体（``Message``、``ToolCallRecord``、``Observation``、``Session``）属于
  ``app/common/model``（C 维护），本包用结构化 Protocol 声明所需形状，不重复定义。

过渡说明：``runtime.py`` 是 nano-codex 原型的注入点，仅在 PR2 剪切前供旧代码使用，
   新代码一律从这里导入。
"""

from app.common.api.agent_loop import AgentLoop, TurnOutcome
from app.common.api.agents_md_loader import AgentsDocument, AgentsMdLoader
from app.common.api.approval_service import ApprovalDecision, ApprovalRequest, ApprovalService
from app.common.api.audit_service import AuditEntry, AuditService
from app.common.api.context_manager import ContextManager, PreparedContext
from app.common.api.llm_provider import CompletionRequest, LLMProvider, ModelResponse, ModelToolCall
from app.common.api.rollback_service import Checkpoint, RollbackResult, RollbackService
from app.common.api.state_store import (
    MessageView,
    SessionView,
    StateStore,
    ToolCallStatus,
    ToolCallView,
    TurnState,
    TurnStatus,
)
from app.common.api.subagent_scheduler import SubagentResult, SubagentScheduler, SubagentTask
from app.common.api.tool import Tool, ToolResult, ToolSpec
from app.common.api.tool_registry import ToolRegistry

__all__ = [
    "AgentLoop",
    "TurnOutcome",
    "AgentsDocument",
    "AgentsMdLoader",
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalService",
    "AuditEntry",
    "AuditService",
    "ContextManager",
    "PreparedContext",
    "CompletionRequest",
    "LLMProvider",
    "ModelResponse",
    "ModelToolCall",
    "Checkpoint",
    "RollbackResult",
    "RollbackService",
    "MessageView",
    "SessionView",
    "StateStore",
    "ToolCallStatus",
    "ToolCallView",
    "TurnState",
    "TurnStatus",
    "SubagentResult",
    "SubagentScheduler",
    "SubagentTask",
    "Tool",
    "ToolResult",
    "ToolSpec",
    "ToolRegistry",
]
