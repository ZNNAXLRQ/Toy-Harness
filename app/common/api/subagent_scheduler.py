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


class SubagentScheduler(Protocol):
    async def spawn(self, task: SubagentTask) -> SubagentResult:
        """执行一个子任务并等它结束；超并发/深度限制抛 ``SubagentError``。"""
        ...

    async def running(self, session_id: str) -> Sequence[SubagentTask]:
        """当前仍在运行的子任务，用于 UI 显示和取消。"""
        ...

    async def cancel(self, run_id: str) -> None: ...
