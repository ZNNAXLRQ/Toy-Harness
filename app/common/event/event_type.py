"""事件类型与 payload 契约。

``EventType`` 是 SSE 的 ``event:`` 字段，也是前端 switch 的契约；**改名视为破坏性
变更**。与原型相比有两处有意改名（``user_query→user_message``、
``agent_end→turn_completed/turn_failed``），因为原型的 ``agent_end`` 同时表达
成功与失败两种语义。

每个事件的 payload 字段在下面两张表里写死：
- ``PAYLOAD_FIELDS``：该事件允许出现的键（防止各层随手塞敏感字段或前端读不到）；
- ``REQUIRED_PAYLOAD_FIELDS``：必须出现的键（前端可以据此直接取用）。
``AgentEvent`` 构造时会强制校验，并统一做一次脱敏（见 agent_event.py）。
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


PAYLOAD_FIELDS: dict[EventType, frozenset[str]] = {
    EventType.USER_MESSAGE: frozenset({"message_id", "content"}),
    EventType.MODEL_RESPONSE: frozenset({"iteration", "content", "finish_reason", "tool_calls"}),
    EventType.TOOL_START: frozenset({"iteration", "call_id", "name", "arguments"}),
    EventType.TOOL_END: frozenset(
        {"call_id", "name", "status", "result", "elapsed_ms", "approved"}
    ),
    EventType.TOOL_SKIPPED: frozenset({"call_id", "name", "reason"}),
    EventType.APPROVAL_REQUIRED: frozenset(
        {"approval_id", "call_id", "tool_name", "arguments", "description"}
    ),
    EventType.APPROVAL_DECIDED: frozenset({"approval_id", "call_id", "decision", "actor"}),
    EventType.TURN_COMPLETED: frozenset({"iterations", "tokens", "content"}),
    EventType.TURN_FAILED: frozenset({"error", "unknown_calls"}),
    EventType.SUBAGENT_STARTED: frozenset({"run_id", "task_id", "prompt"}),
    EventType.SUBAGENT_FINISHED: frozenset({"run_id", "status", "summary", "tokens"}),
    EventType.CHECKPOINT_CREATED: frozenset({"checkpoint_id", "seq", "description"}),
    EventType.ROLLBACK_PERFORMED: frozenset({"checkpoint_id", "reverted", "failed"}),
}

REQUIRED_PAYLOAD_FIELDS: dict[EventType, frozenset[str]] = {
    EventType.USER_MESSAGE: frozenset({"content"}),
    EventType.MODEL_RESPONSE: frozenset({"iteration"}),
    EventType.TOOL_START: frozenset({"call_id", "name"}),
    EventType.TOOL_END: frozenset({"call_id", "status"}),
    EventType.TOOL_SKIPPED: frozenset({"call_id", "reason"}),
    EventType.APPROVAL_REQUIRED: frozenset({"approval_id", "call_id"}),
    EventType.APPROVAL_DECIDED: frozenset({"approval_id", "decision"}),
    EventType.TURN_COMPLETED: frozenset({"iterations"}),
    EventType.TURN_FAILED: frozenset({"error"}),
    EventType.SUBAGENT_STARTED: frozenset({"run_id"}),
    EventType.SUBAGENT_FINISHED: frozenset({"run_id", "status"}),
    EventType.CHECKPOINT_CREATED: frozenset({"checkpoint_id"}),
    EventType.ROLLBACK_PERFORMED: frozenset({"checkpoint_id"}),
}
