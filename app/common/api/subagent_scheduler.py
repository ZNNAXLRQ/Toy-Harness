"""子代理端口：独立上下文、受限工具集、结果汇总回父会话。

三条边界，避免子代理变成「第二个不受控的主循环」：

1. **工具白名单**：子代理只能拿到 ``allowed_tools``，默认只读；写操作要么禁止，
   要么仍然走同一个 ``ApprovalService``（禁止子代理自我批准）。
2. **深度限制**：``max_depth`` 防止子代理再生成子代理形成递归炸弹。
3. **预算独立**：子代理有自己的 token 预算，用尽即失败并回报，不能无限制消耗
   父会话的额度。

## 运行记录与恢复

``subagent_runs`` 是运行状态的**唯一记录**。执行前先落 ``running``，结束后更新为
终态；进程崩溃时遗留的 ``running`` 行在恢复时由调度器判为 ``unknown``（与 tool_call
的 UNKNOWN 同构），**不自动重试**——子代理可能已经产生了外部副作用。
``running()`` 的返回必须能从 ``SubagentRunView`` 重建，不能只存在于内存。
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence


class SubagentRunStatus(StrEnum):
    """``subagent_runs.status`` 的合法取值。

    - ``running``：已落库、执行中；
    - ``success`` / ``failed`` / ``cancelled``：终态；
    - ``unknown``：进程崩溃遗留的 ``running``，恢复时改判，**不自动重试**。
    """

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


TERMINAL_SUBAGENT_STATUSES = frozenset(
    {SubagentRunStatus.SUCCESS, SubagentRunStatus.FAILED, SubagentRunStatus.CANCELLED, SubagentRunStatus.UNKNOWN}
)


@dataclass(frozen=True)
class SubagentTask:
    id: str
    parent_session_id: str
    parent_call_id: str
    prompt: str
    allowed_tools: Sequence[str] = ()
    max_iterations: int = 8
    depth: int = 1
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SubagentResult:
    run_id: str
    status: str
    """``success`` / ``failed`` / ``cancelled`` / ``unknown``。"""
    summary: str
    tokens: int = 0
    steps: int = 0
    detail: Mapping[str, Any] | None = None


class SubagentRunView(Protocol):
    """``subagent_runs`` 表的一行（C 的 dataclass 自动满足）。"""

    id: str
    session_id: str
    parent_task_id: str | None
    status: str
    context_json: str | None
    result_json: str | None
    created_at: int


class SubagentRunStore(Protocol):
    """子代理运行的持久化端口（C 实现，A 的 ``SubagentScheduler`` 消费）。

    没有它，调度器只能把运行状态留在内存里：重启后 UI 看不到「有个子代理曾经在跑」，
    也无法判断它是完成、失败还是未知。
    """

    async def save_run(
        self,
        run_id: str,
        session_id: str,
        *,
        parent_task_id: str | None = None,
        status: str = "running",
        context_json: str | None = None,
    ) -> SubagentRunView: ...

    async def update_run(
        self, run_id: str, *, status: str, result_json: str | None = None
    ) -> SubagentRunView: ...

    async def load_run(self, run_id: str) -> SubagentRunView | None: ...

    async def list_runs(self, session_id: str) -> Sequence[SubagentRunView]: ...


class SubagentScheduler(Protocol):
    async def spawn(self, task: SubagentTask) -> SubagentResult:
        """执行一个子任务并等它结束；超并发/深度限制抛 ``SubagentError``。

        实现需要：``LLMProvider``、只读的 ``ToolRegistry`` 视图、``ApprovalService``
        （仍需人工批准敏感操作）以及 ``SubagentRunStore``（先落运行记录再执行，
        结束后更新状态；崩溃时把它留在 unknowns 里由人工处理）。
        """
        ...

    async def running(self, session_id: str) -> Sequence[SubagentRunView]:
        """当前仍在运行（或崩溃后判为 unknown 待确认）的子任务。"""
        ...

    async def cancel(self, run_id: str) -> None:
        """请求取消一个运行。

        语义：``cancel`` 只置取消标志并把 run 记为 ``cancelled``（若尚未终态）；
        正在等待的 ``spawn`` 以 ``SubagentResult(status="cancelled")`` **正常返回**
        （不抛 ``asyncio.CancelledError``），便于父循环把结果写回父会话。
        已终态的 run 调 ``cancel`` 是空操作。
        """
        ...
