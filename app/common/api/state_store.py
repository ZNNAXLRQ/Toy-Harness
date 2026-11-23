"""状态端口：会话、消息、工具调用、观察的唯一真相来源。

实现方（C）用 SQLite 落库；本模块只声明**形状**与**不变式**，不含实现。

四条不变式（实现必须保证，测试按此验收）：

1. ``append_message`` 分配会话内单调递增的 ``seq``，从 1 开始，不留空洞。
2. ``finish_tool_call`` 在**同一事务**里写 observation、追加 ``role="tool"`` 消息并把
   状态置为终态；三者要么全成、要么全不变。序列化失败等异常不得留下半个结果
   （「已完成但没有输出」）。
3. 有 ``dispatched`` 但没有 observation 的调用，恢复时一律判 ``unknown``，
   **绝不自动重放**——外部副作用可能已经发生。
4. 同一会话同一时刻最多一个 open turn；违反时抛 ``StateError``。

实体用 Protocol 声明，C 的 dataclass 天然满足（结构化类型，无需继承）。

``TurnState`` 是**投影**：由 ``load_open_turn`` 从 messages + tool_calls 重建，
不单独存表、也没有 ``save_turn``。重建规则：

- 有 ``awaiting_approval`` 的调用 → ``WAITING_APPROVAL``（带 ``pending_call_id``）；
- 有 ``dispatched`` 且无观察结果 → 判 ``unknown`` 后返回 ``FAILED``（``notes``
  里列出这些调用，交给人工确认，绝不重放）；
- 还有 ``pending``/``approved`` 的调用 → ``OPEN``；
- 否则看最后一条消息：**不是 assistant** → ``OPEN``（用户消息已落但模型未回复，
  或工具结果已落等待下一次模型调用，两种中断都能接续）；是 assistant → ``None``；
- 没有消息也没有调用 → ``None``。

``iteration`` = 本轮（最后一条 user 消息之后）assistant 消息的条数，可从表里数出来。
**token 用量不持久化**：schema 没有对应列，恢复后按 0 重新计；跨重启的预算连续性是
C 的 schema 决策，不在本端口里假装已解决。
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, AsyncContextManager, Protocol, Sequence

from app.common.api.llm_provider import ModelToolCall
from app.common.api.tool import ToolResult


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


class TurnStatus(StrEnum):
    """一轮对话的状态；``waiting_approval`` 是唯一的可恢复挂起点。"""

    OPEN = "open"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


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


class StateStore(Protocol):
    """持久化端口。所有方法都可能抛 ``StateError``/``NotFoundError``。"""

    # --- 会话 ---
    async def create_session(
        self, *, title: str | None = None, agents_md_version: str | None = None
    ) -> SessionView: ...

    async def load_session(self, session_id: str) -> SessionView | None: ...

    async def update_session(self, session_id: str, **fields: Any) -> SessionView: ...

    async def list_sessions(self, limit: int = 50) -> Sequence[SessionView]: ...

    # --- 消息 ---
    async def append_message(
        self,
        session_id: str,
        role: str,
        content: str | None,
        *,
        tool_call_id: str | None = None,
    ) -> MessageView: ...

    async def list_messages(
        self, session_id: str, *, after_seq: int = 0
    ) -> Sequence[MessageView]: ...

    # --- 工具调用与观察 ---
    async def record_tool_calls(
        self,
        session_id: str,
        message_id: str | None,
        calls: Sequence[ModelToolCall],
    ) -> Sequence[ToolCallView]: ...

    async def set_tool_status(
        self,
        call_id: str,
        status: ToolCallStatus,
        *,
        error: str | None = None,
    ) -> ToolCallView: ...

    async def finish_tool_call(self, call_id: str, result: ToolResult) -> ToolCallView:
        """写 observation、置终态，并追加对应的 ``role="tool"`` 消息——三者同一事务。

        合成一条操作的原因是顺序：工具消息的 ``seq`` 必须落在调用发生的位置，
        让上层分两次写会给出交错的序号。

        结果无法序列化时抛 ``StateError``，且调用状态与消息表**保持原样**；
        实现必须先完成（可能失败的）序列化，再动状态。
        """
        ...

    async def list_tool_calls(self, session_id: str) -> Sequence[ToolCallView]: ...

    # --- 一轮 ---
    async def load_open_turn(self, session_id: str) -> TurnState | None:
        """按模块开头的规则重建未完成的一轮；没有 open turn 时返回 ``None``。

        投影是只读重建：实现**不得**在这里写入新表，也不存在对称的 ``save_turn``。
        需要结束一轮（例如手动放弃）时用 ``update_session(status=...)``。
        """
        ...

    # --- 事务 ---
    def transaction(self) -> AsyncContextManager[None]:
        """显式事务；不支持嵌套，实现可把内层当作最外层的一部分。"""
        ...
