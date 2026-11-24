"""跨模块抽象接口（端口）。

规则：``common`` 不 import 任何实现层；实现层只实现这里的 Protocol。

- 端口 DTO（``ModelResponse``、``CompletionRequest`` 等）放在本包，它们是**不落库**的
  传输形状。
- **落库实体一律用结构化 Protocol 视图表示**（``SessionView`` / ``MessageView`` /
  ``ToolCallView`` / ``ApprovalView``），由 C 的 ``common/model`` 数据类自动满足；
  不在这里定义任何落库实体的 dataclass。这条规则是本轮审计后修正的：
  之前 ``ApprovalRequest``/``Checkpoint``/``SubagentRunRecord`` 是 dataclass，
  与"实体归 C"的分工矛盾。

持久化端口按聚合拆开：会话/轮次/工具调用/审批归 ``StateStore``，检查点归
``CheckpointStore``，子代理运行归 ``SubagentRunStore``。三个都由 C 实现。

过渡说明：``runtime.py`` 是 nano-codex 原型的注入点，仅在 PR2 剪切前供旧代码使用，
   新代码一律从这里导入。
"""

from app.common.api.agent_loop import AgentLoop, TurnOutcome
from app.common.api.agents_md_loader import AgentsDocument, AgentsMdLoader
from app.common.api.approval_service import ApprovalService
from app.common.api.audit_service import AuditService
from app.common.api.context_manager import ContextManager, PreparedContext
from app.common.api.llm_provider import CompletionRequest, LLMProvider, ModelResponse, ModelToolCall
from app.common.api.rollback_service import (
    CheckpointStore,
    CheckpointView,
    RollbackResult,
    RollbackService,
)
from app.common.api.state_store import (
    OPEN_TOOL_STATUSES,
    TOOL_TRANSITIONS,
    ApprovalDecision,
    ApprovalStatus,
    ApprovalView,
    AssistantTurn,
    AuditView,
    MessageRole,
    MessageView,
    SessionStatus,
    SessionView,
    StateStore,
    ToolCallStatus,
    ToolCallView,
    TurnState,
    TurnStatus,
)
from app.common.api.subagent_scheduler import (
    SubagentResult,
    SubagentRunStatus,
    SubagentRunStore,
    SubagentRunView,
    SubagentScheduler,
    SubagentTask,
)
from app.common.api.tool import Tool, ToolResult, ToolSpec
from app.common.api.tool_registry import ToolRegistry

__all__ = [
    "AgentLoop",
    "TurnOutcome",
    "AgentsDocument",
    "AgentsMdLoader",
    "ApprovalDecision",
    "ApprovalStatus",
    "ApprovalService",
    "ApprovalView",
    "AuditView",
    "AuditService",
    "ContextManager",
    "PreparedContext",
    "CompletionRequest",
    "LLMProvider",
    "ModelResponse",
    "ModelToolCall",
    "CheckpointStore",
    "CheckpointView",
    "RollbackResult",
    "RollbackService",
    "OPEN_TOOL_STATUSES",
    "TOOL_TRANSITIONS",
    "AssistantTurn",
    "MessageRole",
    "MessageView",
    "SessionStatus",
    "SessionView",
    "StateStore",
    "ToolCallStatus",
    "ToolCallView",
    "TurnState",
    "TurnStatus",
    "SubagentResult",
    "SubagentRunStatus",
    "SubagentRunStore",
    "SubagentRunView",
    "SubagentScheduler",
    "SubagentTask",
    "Tool",
    "ToolResult",
    "ToolSpec",
    "ToolRegistry",
]
