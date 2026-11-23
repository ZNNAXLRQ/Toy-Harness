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

**两种状态只能由专门的写入方法产生，``set_tool_status`` 一律拒绝**：

- ``awaiting_approval`` ← ``record_approval_request``（审批行与状态同事务）；
- ``approved`` / ``dispatched`` ← ``claim_tool_call`` 的条件迁移。

这样"等待审批但没有审批记录"这种不可恢复状态在实现上就无法被构造出来。

**只有通过 ``claim_tool_call`` 条件迁移（CAS）的那一方可以执行工具**：并发恢复时
第二个调用者拿到 ``False``，因此不存在重复派发。``claim_tool_call`` 在
SQLite 实现里必须是**单条条件更新**（``UPDATE ... WHERE status = :from``，按
``rowcount`` 判定），不得读-改-写。

``approved`` 不等于可执行：恢复流程用两次 CAS 完成一次性派发——
``awaiting_approval → approved``（抢到的人消费审批）、``approved → dispatched``
（抢到的人执行）。第二次失败就说明别人已经派发过，必须放弃。
**消费那次 CAS 必须校验审批记录确为 approved**：没有已批准的审批就不许进入
``approved``，否则未批准的工具会被执行。

**审批拒绝即终止本轮。** ``denied`` 是安全边界：模型不能靠「换个工具再来」绕开拒绝。
因此一轮里只要出现 ``denied`` 调用，恢复推导就认为该轮没有 open turn（见下），
不需要依赖状态字段恰好被写对。

## 一轮的边界与恢复

``TurnState`` 是**投影**，没有 ``save_turn``。**只扫描本轮**（最后一条 ``user`` 消息之后
产生的调用），上一轮遗留的调用不参与本轮判定。边界规则：

- ``sessions.status`` 为 ``failed`` / ``abandoned`` → 没有 open turn（``None``）。
  只有这两个是硬终态；``idle`` / ``completed`` 不阻断推导——一轮完成后用户再发消息
  就是新一轮。
- 本轮存在 ``dispatched`` 而无观察结果 → 判 ``unknown``，落会话 ``failed``，返回带
  ``notes["unknown_calls"]`` 的 ``FAILED``（**唯一一处恢复写**，幂等，判过即不可恢复）。
  注意：这是**破坏性**判定，只允许由「启动恢复」入口独占调用；普通查询应使用
  ``peek_open_turn``（见 ``AgentLoop`` 的调用者约束）。
- 本轮出现过 ``denied`` 调用 → 没有 open turn（``None``）；拒绝是安全边界。
  （判定顺序如此：拒绝的优先级高于「还有调用在等审批」，两者同时出现是异常状态，
  按终止处理。）
- 本轮存在 ``awaiting_approval``：
  - 审批记录已决策（approved/denied）→ ``WAITING_APPROVAL``，同时给出
    ``pending_call_id`` 与 ``pending_approval_id``（重启后据此恢复）；
  - 未决策 → ``WAITING_APPROVAL``，``pending_approval_id=None``。
  **同一时刻最多一条**；实现发现两条及以上必须抛 ``StateError``。
  （``awaiting_approval`` 只能由 ``record_approval_request`` 产生，所以这里的
  "未决策"必然能找到审批行；找不到说明实现漏了事务。）
- 本轮存在 ``pending`` / ``approved`` 的调用 → ``OPEN``（模型已回、工具未派发，
  或审批已消费待派发），**并在 ``open_call_ids`` 里给出这些调用**。``approved``
  表示审批已消费、等派发，loop 恢复时按 ``open_call_ids`` 用
  ``claim_tool_call(approved→dispatched)`` 抢占执行权。
- 以上都不满足时看最后一条**非 summary/system** 消息：``user`` / ``tool`` → ``OPEN``
  （模型还没回复，或工具结果已落等待下一次模型调用）；``assistant`` → ``None``。
  ``summary`` 是压缩注记，既不能重开已结束的轮次，也不能把进行中的轮次误判为结束。

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
    ToolCallStatus.APPROVED: frozenset({ToolCallStatus.DISPATCHED}),
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

OPEN_TOOL_STATUSES = frozenset(
    {
        ToolCallStatus.PENDING,
        ToolCallStatus.AWAITING_APPROVAL,
        ToolCallStatus.APPROVED,
        ToolCallStatus.DISPATCHED,
    }
)
"""还没结束的状态；实现用它在「未终态」和「已终态」之间区分。"""


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


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class ApprovalDecision(StrEnum):
    """人工决策的取值（不含 pending）。放在这里是为了避免 api 包内循环 import。"""

    APPROVED = "approved"
    DENIED = "denied"


class ApprovalView(Protocol):
    """``approvals`` 表的一行（schema.sql 新增表；C 的 dataclass 自动满足）。"""

    id: str
    session_id: str
    call_id: str
    tool_name: str
    arguments: str | None
    """模型给的原始 JSON 文本，与 ``ToolCallView.arguments`` 一致，不提前解析。"""
    description: str | None
    status: str
    decided_by: str | None
    created_at: int
    decided_at: int | None


class AuditView(Protocol):
    """``audit_logs`` 表的一行（C 的 dataclass 自动满足）。

    ``detail`` 是 JSON 文本（或纯字符串），由写入方序列化、读取方解析；
    **审计内容必须经 ``redact`` 处理**（见 ``AuditService``）。
    """

    id: str
    session_id: str | None
    actor: str
    action: str
    detail: str | None
    created_at: int


@dataclass
class TurnState:
    """一轮执行的投影，由 ``StateStore.load_open_turn`` 重建（不落库）。"""

    session_id: str
    status: TurnStatus
    iteration: int = 0
    """本轮已发生的模型调用次数，由 assistant 消息条数数出来。"""
    pending_call_id: str | None = None
    pending_approval_id: str | None = None
    """挂起时对应的审批记录 id；已决策未消费时用于重启恢复。"""
    open_call_ids: tuple[str, ...] = ()
    """本轮还没终态的调用 id（``pending`` / ``approved`` / 在飞 ``dispatched``）。

    ``OPEN`` 态必须靠它恢复：loop 按顺序对每个 id 重新做 CAS，不能重新推导轮次。
    """
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
        """更新会话字段（含 ``status``）。改 ``status`` 是 loop 的职责。

        - ``id`` 不可修改（实现应抛 ``StateError``）：它同时是主键与索引键，
          改了会让会话查不到自己。
        - ``status`` 必须是 ``SessionStatus`` 的取值，否则 ``StateError``；
          写进去的垃圾字符串会让恢复逻辑的终态判断失效。
        """
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
        """追加 user / summary / system 等消息；工具消息由 ``finish_tool_call`` 负责。

        会话处于 ``failed`` / ``abandoned`` 时抛 ``ConflictError``（不能往终止的轮次里
        追加消息）。
        """
        ...

    async def record_assistant_turn(
        self, session_id: str, response: ModelResponse
    ) -> AssistantTurn:
        """一次写入 assistant 消息与它请求的全部 tool_calls。

        ``reasoning_content`` 不落库（schema 无列）；重复的 call id、或与已存在
        call id 冲突时抛 ``StateError``，且**不留下半条消息**。
        会话处于 ``failed`` / ``abandoned`` 时抛 ``ConflictError``。
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
    ) -> ToolCallView:
        """按 ``TOOL_TRANSITIONS`` 迁移；终态迁出、非法迁移抛 ``StateError``。

        **不允许**迁移到 ``awaiting_approval`` / ``approved`` / ``dispatched``
        （均抛 ``StateError``）：等待审批只能由 ``record_approval_request`` 产生，
        approved/dispatched 只能由 ``claim_tool_call`` 产生。否则可以造出
        「等待审批但没有审批记录」的不可恢复状态。
        """
        ...

    async def claim_tool_call(
        self,
        call_id: str,
        *,
        from_status: ToolCallStatus,
        to_status: ToolCallStatus,
    ) -> bool:
        """条件迁移（CAS）：当前状态等于 ``from_status`` 才改成 ``to_status``。

        只允许两种用途：
        - 消费审批：``awaiting_approval → approved``——**必须校验审批记录已为
          approved**，否则抛 ``ConflictError``；没有这条校验，未批准的工具可以直接
          被消费并执行。
        - 派发执行：``approved → dispatched`` / ``pending → dispatched``。

        返回 ``True`` 的那一方是**唯一**允许继续的人；并发恢复的其余调用者拿到
        ``False``，不会重复消费审批或重复派发。**会话已处于 ``failed``/``abandoned``
        时抛 ``ConflictError``**——否则副作用会发生而没有观察结果可以落。
        其他迁移（拒绝、失败、完成）一律走 ``finish_tool_call``，
        传非法 ``from/to`` 组合必须抛 ``StateError``。

        SQLite 实现必须用单条条件更新判定，不得先查后写：

            UPDATE tool_calls
               SET status = :to
             WHERE id = :id AND status = :from
               AND (:to <> 'approved' OR EXISTS (
                     SELECT 1 FROM approvals WHERE call_id = :id AND status = 'approved'))

        ``rowcount == 1`` 即抢到。
        """
        ...

    async def finish_tool_call(self, call_id: str, result: ToolResult) -> ToolCallView:
        """写 observation、``reversible``、追加 ``role="tool"`` 消息并置终态——同一事务。

        ``result.status`` 决定目标状态（success→completed / error→failed /
        denied→denied）。``denied`` 允许从 ``pending`` 或 ``awaiting_approval`` 直接
        转入（工具从未被派发）；``dispatched`` 不能改判为 ``denied``。
        observation 的持久化内容是 ``{"status", "result", "receipt"}`` 的 JSON，
        ``receipt`` 由 ``get_receipt`` 读回。

        会话已处于 ``failed`` / ``abandoned`` 时必须抛 ``ConflictError``：迟到的
        工具结果不能把已经终止的一轮「救活」，那会制造出与最终状态矛盾的证据。
        """
        ...

    async def list_tool_calls(self, session_id: str) -> Sequence[ToolCallView]:
        """按创建顺序返回该会话的全部工具调用（回滚据此计算逆序补偿）。"""
        ...

    async def get_receipt(self, call_id: str) -> str | None:
        """读回成功调用的补偿凭据（``reversible`` 为真时才有）。"""
        ...

    # --- 审批记录（approvals 表） ---
    async def record_approval_request(
        self,
        session_id: str,
        call_id: str,
        tool_name: str,
        arguments: str | None,
        description: str | None,
    ) -> ApprovalView:
        """同事务完成两件事：插审批行（status=pending）+ 把调用置 ``awaiting_approval``。

        按 ``call_id`` 唯一：重复调用直接返回已有记录（幂等），不改状态。
        调用必须处于 ``pending`` 且属于同一会话，否则抛 ``StateError``；
        ``tool_name`` / ``arguments`` 应与该 tool_call 行一致，不一致抛 ``StateError``
        （防止审批记录与真实调用对不上）。
        会话处于 ``failed`` / ``abandoned`` 时抛 ``ConflictError``。
        """
        ...

    async def decide_approval(
        self, approval_id: str, decision: ApprovalDecision, *, actor: str
    ) -> ApprovalView:
        """同事务写入决策（approved/denied）与 ``audit_logs`` 审计行。

        - 已决策的记录再决策抛 ``ApprovalError``；
        - 调用已不是 ``awaiting_approval``（例如已被派发）时抛 ``ConflictError``；
        - 会话处于 ``failed`` / ``abandoned`` 时抛 ``ConflictError``；
        - **本方法不改 tool_calls 状态**——审批消费是一次独立的 CAS
          （``awaiting_approval → approved``），避免"决策成功但没人执行"或
          "执行了但没有决策记录"。
        - 审计行必须是：``action='approval_decided'``、``actor=决策人``、
          ``detail`` 含 ``approval_id`` 与 ``decision``（JSON 文本）。
        """
        ...

    async def get_approval(self, approval_id: str) -> ApprovalView | None: ...

    async def get_approval_for_call(self, call_id: str) -> ApprovalView | None:
        """按调用找审批（**含已决策**）：恢复时判断「已决策未消费」。"""
        ...

    async def list_pending_approvals(
        self, session_id: str | None = None
    ) -> Sequence[ApprovalView]:
        """未决策的审批队列，按 ``created_at`` 升序；Web 用它渲染审批列表。"""
        ...

    # --- 审计（audit_logs 表） ---
    async def record_audit(
        self,
        *,
        action: str,
        actor: str,
        session_id: str | None = None,
        detail: str | None = None,
    ) -> AuditView:
        """写一条审计。``detail`` 是已经序列化好（且已脱敏）的文本。

        审计是**不可变**的：没有对应的 update/delete 方法。写失败必须抛异常，
        不允许静默吞掉——「查不到谁批的」比「turn 失败」更严重。
        """
        ...

    async def query_audits(
        self, *, session_id: str | None = None, limit: int = 100
    ) -> Sequence[AuditView]:
        """按 ``created_at`` 升序返回审计；``session_id=None`` 表示全部。

        这个方法是审计不变式的**验收通道**：``decide_approval`` 之后必须能查到
        ``approval_decided`` 行。
        """
        ...

    async def latest_message_seq(self, session_id: str) -> int:
        """会话当前最大消息序号（没有消息时 0）。检查点用它确定打点位置。"""
        ...

    # --- 一轮 ---
    async def load_open_turn(self, session_id: str) -> TurnState | None:
        """按模块开头的规则重建未完成的一轮；没有 open turn 时返回 ``None``。

        这是**恢复入口**，也是唯一允许写状态的地方：判定 ``unknown`` 时会把会话置为
        ``failed``（幂等，判过一次就不可再恢复）。需要结束一轮时由 loop 调
        ``update_session(status=...)``。

        **调用者约束**：只允许「启动恢复」与 loop 的 turn 入口调用，且同一会话内串行；
        普通查询（例如 Web 的 GET 会话）必须用 ``peek_open_turn``，否则会把正在执行的
        ``dispatched`` 调用误判成 ``unknown``。
        """
        ...

    async def peek_open_turn(self, session_id: str) -> TurnState | None:
        """只读投影：规则与 ``load_open_turn`` 相同，但**不写任何东西**。

        ``dispatched`` 且无观察结果的调用在 notes["in_flight"] 里报告，不判 UNKNOWN。
        UI/只读接口用它；恢复判定用 ``load_open_turn``。
        """
        ...

    # --- 事务 ---
    def transaction(self) -> AsyncContextManager[None]:
        """显式事务：块内抛异常时，块内已完成的所有写入必须回滚。

        不支持嵌套，实现可把内层当作最外层的一部分。
        """
        ...
