"""子代理端口：独立上下文、受限工具集、结果汇总回父会话。

三条边界，避免子代理变成「第二个不受控的主循环」：

1. **工具白名单**：子代理只能拿到 ``allowed_tools``，默认只读；写操作要么禁止，
   要么仍然走同一个 ``ApprovalService``（禁止子代理自我批准）。
2. **深度限制**：``max_depth`` 防止子代理再生成子代理形成递归炸弹。
3. **预算独立**：子代理有自己的 token 预算，用尽即失败并回报，不能无限制消耗
   父会话的额度。

子代理的对话过程不保证完整落库（schema 无 parent 列）；落库的是 ``subagent_runs``
里的任务摘要与结果，工具调用仍记在父会话审计下。
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence


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


@dataclass(frozen=True)
class SubagentRunRecord:
    """``subagent_runs`` 表的一行（C 的 schema）。"""

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
    也无法判断它是完成、失败还是未知（对应 ``subagent_runs`` 表的三列）。
    """

    async def save_run(self, record: SubagentRunRecord) -> SubagentRunRecord: ...

    async def update_run(
        self, run_id: str, *, status: str, result_json: str | None
    ) -> SubagentRunRecord: ...

    async def load_run(self, run_id: str) -> SubagentRunRecord | None: ...

    async def list_runs(self, session_id: str) -> Sequence[SubagentRunRecord]: ...


class SubagentScheduler(Protocol):
    async def spawn(self, task: SubagentTask) -> SubagentResult:
        """执行一个子任务并等它结束；超并发/深度限制抛 ``SubagentError``。

        实现需要：``LLMProvider``、只读的 ``ToolRegistry`` 视图、``ApprovalService``
        （仍需人工批准敏感操作）以及 ``SubagentRunStore``（先落运行记录再执行，
        结束后更新状态；崩溃时把它留在 unknowns 里由人工处理）。
        """
        ...

    async def running(self, session_id: str) -> Sequence[SubagentTask]:
        """当前仍在运行的子任务，用于 UI 显示和取消。"""
        ...

    async def cancel(self, run_id: str) -> None: ...
