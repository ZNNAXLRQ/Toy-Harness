"""事件类型。作为 SSE 的 ``event:`` 字段和前端 switch 的契约，改名视为破坏性变更。

命名对齐原型已经发过的事件（``user_query`` / ``model_response`` / ``tool_start`` /
``tool_end`` / ``tool_skipped`` / ``agent_end``），新增的审批、子代理、检查点事件
沿用同一风格。
"""

from enum import StrEnum


class EventType(StrEnum):
    # 会话与消息
    USER_MESSAGE = "user_message"
    MODEL_RESPONSE = "model_response"

    # 工具
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    TOOL_SKIPPED = "tool_skipped"

    # 审批
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_DECIDED = "approval_decided"

    # 一轮的结果
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"

    # 子代理
    SUBAGENT_STARTED = "subagent_started"
    SUBAGENT_FINISHED = "subagent_finished"

    # 检查点与回滚
    CHECKPOINT_CREATED = "checkpoint_created"
    ROLLBACK_PERFORMED = "rollback_performed"
