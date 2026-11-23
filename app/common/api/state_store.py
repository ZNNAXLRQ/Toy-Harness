"""状态端口：会话、消息、工具调用、观察的唯一真相来源。

实现方（C）用 SQLite 落库；本模块只声明**形状**与**不变式**，不含实现。

## 写入边界

1. ``append_message`` 分配会话内单调递增的 ``seq``，从 1 开始，不留空洞。
2. **assistant 消息与它请求的 tool_calls 必须一次写入**（``record_assistant_turn``）。
   分成两次写，崩在中间就会得到「一条没有工具调用的 assistant 消息」——恢复时
   会被误判为「这一轮已经结束」，从而丢掉本该执行的工具。这是本轮评审复现的问题。
3. ``finish_tool_call`` 在**同一事务**里写 observation、追加 ``role="tool"`` 消息、
   置终态、写 ``reversible``；三者要么全成、要么全不变。结果无法序列化时抛
   ``StateError`` 且保持原样。
4. 有 ``dispatched`` 但没有 observation 的调用，恢复时一律判 ``unknown``，并**持久化**
   会话终态 ``failed``——判定必须幂等，否则第二次读取会把这轮重新打开，等于重放。

## 状态机

工具调用只允许这些迁移（见 ``TOOL_TRANSITIONS``），终态不可再迁出：

    pending ─→ awaiting_approval ─→ approved ─→ dispatched ─→ completed | failed
       │              │                              
       └──────────────┴─→ denied（审批拒绝，工具从未派发）

**只有通过 ``claim_tool_call`` 条件迁移（CAS）的那一方可以执行工具**：并发恢复时
第二个调用者拿到 ``False``，因此不存在重复派发。``set_tool_status`` 直接拒绝
终态迁出。

## 一轮的边界与恢复

``TurnState`` 是**投影**，没有 ``save_turn``。边界规则：

- ``sessions.status`` 为 ``failed`` / ``abandoned`` → 没有 open turn（``None``）。
  只有这两个是「硬终态」；``idle`` / ``completed`` 不阻断推导——一轮完成后用户再发
  消息就是新一轮，靠状态字段判断反而会把新轮挡掉。
- 存在 ``awaiting_approval`` → ``WAITING_APPROVAL``；
- 存在 ``dispatched`` 而无观察结果 → 判 ``unknown``，落会话 ``failed``，返回带
  ``notes["unknown_calls"]`` 的 ``FAILED``（**唯一一处恢复写**，幂等，判过即不可恢复）；
- 其余情况看最后一条消息：``user`` / ``tool`` → ``OPEN``（模型还没回复，或工具结果
  已落等待下一次模型调用）；``assistant`` / ``summary`` / ``system`` → ``None``。
  ``summary`` 不能重新打开已结束的轮次。

``iteration`` = 本轮（最后一条 ``user`` 消息之后）assistant 消息条数，可从表里数出来。
**token 用量与 ``reasoning_content`` 都不持久化**：schema 没有对应列。恢复后的历史
只有可见 content；跨重启的预算连续性与推理内容保留，属于 C 的 schema 决策，
本端口不假装已解决。

实体用 Protocol 声明，C 的 dataclass 天然满足（结构化类型，无需继承）。
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, AsyncContextManager, Protocol, Sequence

from app.common.api.llm_provider import ModelResponse
from app.common.api.tool import ToolResult


class MessageRole(StrEnum):
    """消息角色。``summary`` 是压缩后的历史注记，不构成一轮的开始或结束。"""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SUMMARY = "summary"
    SYSTEM = "system"


class ToolCallStatus(StrEnum):
    """工具调用的生命周期。``unknown`` 只能由恢复流程产生，不能主动写入。"""

    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    DENIED = "denied"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class SessionStatus(StrEnum):
    """会话（也就是一轮）的状态。恢复逻辑以它为准判断「还有没有未完成的轮」。"""

    IDLE = "idle"
    OPEN = "open"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class TurnStatus(StrEnum):
    """``load_open_turn`` 返回的投影状态。"""

    OPEN = "open"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


TOOL_TRANSITIONS: dict[ToolCallStatus, frozenset[ToolCallStatus]] = {
    ToolCallStatus.PENDING: frozenset(
        {ToolCallStatus.AWAITING_APPROVAL, ToolCallStatus.DISPATCHED, ToolCallStatus.DENIED}
    ),
    ToolCallStatus.AWAITING_APPROVAL: frozenset(
        {ToolCallStatus.APPROVED, ToolCallStatus.DENIED}
    ),
    ToolCallStatus.APPROVED: frozenset({ToolCallStatus.DISPATCHED, ToolCallStatus.DENIED}),
    ToolCallStatus.DISPATCHED: frozenset({ToolCallStatus.COMPLETED, ToolCallStatus.FAILED}),
    ToolCallStatus.DENIED: frozenset(),
    ToolCallStatus.COMPLETED: frozenset(),
    ToolCallStatus.FAILED: frozenset(),
    ToolCallStatus.UNKNOWN: frozenset(),
}

TERMINAL_TOOL_STATUSES = frozenset(
    {
        ToolCallStatus.DENIED,
        ToolCallStatus.COMPLETED,
        ToolCallStatus.FAILED,
        ToolCallStatus.UNKNOWN,
    }
)

TERMINAL_SESSION_STATUSES = frozenset({SessionStatus.FAILED, SessionStatus.ABANDONED})
"""只有这两个状态会阻止恢复。``idle`` / ``completed`` 仍按行推导——完成后用户再发
一条消息就是新一轮，不能因为状态字段是 completed 就把新轮挡掉。"""

_OPEN_TOOL_STATUSES = frozenset(
    {
        ToolCallStatus.PENDING,
        ToolCallStatus.AWAITING_APPROVAL,
        ToolCallStatus.APPROVED,
        ToolCallStatus.DISPATCHED,
    }
)
"""可继续推进的状态；实现用它区分「还没结束」和「已终态」。"""


class SessionView(Protocol):
    id: str
    title: str | None
    status: str
    agents_md_version: str | None
    prefix_hash: str | None
    created_at: int
    updated_at: int


class MessageView(Protocol):
    id: str
    session_id: str
    seq: int
    role: str
    content: str | None
    tool_call_id: str | None
    created_at: int


class ToolCallView(Protocol):
    id: str
    session_id: str
    message_id: str | None
    tool_name: str
    arguments: str | None
    status: str
    reversible: bool | None
    created_at: int


@dataclass
class TurnState:
    """一轮执行的投影，由 ``StateStore.load_open_turn`` 重建（不落库）。"""

    session_id: str
    status: TurnStatus
    iteration: int = 0
    """本轮已发生的模型调用次数，由 assistant 消息条数数出来。"""
    pending_call_id: str | None = None
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass
class AssistantTurn:
    """一次 assistant 写入的结果：消息 + 它请求的工具调用（同事务）。"""

    message: MessageView
    tool_calls: Sequence[ToolCallView] = ()


class StateStore(Protocol):
    """持久化端口。所有方法都可能抛 ``StateError``/``NotFoundError``。"""

    # --- 会话 ---
    async def create_session(
        self, *, title: str | None = None, agents_md_version: str | None = None
    ) -> SessionView: ...

    async def load_session(self, session_id: str) -> SessionView | None: ...

    async def update_session(self, session_id: str, **fields: Any) -> SessionView:
        """更新会话字段（含 ``status``）。改 ``status`` 是 loop 的职责。"""
        ...

    async def list_sessions(self, limit: int = 50) -> Sequence[SessionView]: ...

    # --- 消息 ---
    async def append_message(
        self,
        session_id: str,
        role: MessageRole | str,
        content: str | None,
        *,
        tool_call_id: str | None = None,
    ) -> MessageView:
        """追加 user / summary / system 等消息；工具消息由 ``finish_tool_call`` 负责。"""
        ...

    async def record_assistant_turn(
        self, session_id: str, response: ModelResponse
    ) -> AssistantTurn:
        """一次写入 assistant 消息与它请求的全部 tool_calls。

        ``reasoning_content`` 不落库（schema 无列）；重复的 call id、或与已存在
        call id 冲突时抛 ``StateError``，且**不留下半条消息**。
        """
        ...

    async def list_messages(
        self, session_id: str, *, after_seq: int = 0
    ) -> Sequence[MessageView]: ...

    # --- 工具调用与观察 ---
    async def set_tool_status(
        self,
        call_id: str,
        status: ToolCallStatus,
        *,
        error: str | None = None,
    ) -> ToolCallView:
        """按 ``TOOL_TRANSITIONS`` 迁移；终态迁出、非法迁移抛 ``StateError``。"""
        ...

    async def claim_tool_call(
        self,
        call_id: str,
        *,
        from_status: ToolCallStatus,
        to_status: ToolCallStatus,
    ) -> bool:
        """条件迁移（CAS）：当前状态等于 ``from_status`` 才改成 ``to_status``。

        返回 ``True`` 的那一方是**唯一**允许执行工具的人；并发恢复的其余调用者拿到
        ``False``，从而不会重复派发。只允许一次迁移，不做重试。
        """
        ...

    async def finish_tool_call(self, call_id: str, result: ToolResult) -> ToolCallView:
        """写 observation、``reversible``、追加 ``role="tool"`` 消息并置终态——同一事务。

        ``result.status`` 决定目标状态（success→completed / error→failed /
        denied→denied）。``denied`` 允许从 ``pending`` 或 ``awaiting_approval`` 直接
        转入（工具从未被派发）；``dispatched`` 不能改判为 ``denied``。
        observation 的持久化内容是 ``{"status", "result", "receipt"}`` 的 JSON，
        ``receipt`` 由 ``get_receipt`` 读回。
        """
        ...

    async def list_tool_calls(self, session_id: str) -> Sequence[ToolCallView]: ...

    async def get_receipt(self, call_id: str) -> str | None:
        """读回成功调用的补偿凭据（``reversible`` 为真时才有）。"""
        ...

    # --- 一轮 ---
    async def load_open_turn(self, session_id: str) -> TurnState | None:
        """按模块开头的规则重建未完成的一轮；没有 open turn 时返回 ``None``。

        这是**恢复入口**，也是唯一允许写状态的地方：判定 ``unknown`` 时会把会话置为
        ``failed``（幂等，判过一次就不可再恢复）。需要结束一轮时由 loop 调
        ``update_session(status=...)``。
        """
        ...

    # --- 事务 ---
    def transaction(self) -> AsyncContextManager[None]:
        """显式事务：块内抛异常时，块内已完成的所有写入必须回滚。

        不支持嵌套，实现可把内层当作最外层的一部分。
        """
        ...
