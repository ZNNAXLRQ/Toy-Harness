"""委派策略：工具白名单、深度上限、并发上限、token/迭代预算。

策略是**纯数据 + 校验**，不含 I/O；调度器在执行前后调用它，违规一律抛
``SubagentError``（失败要响亮，而不是让子代理悄悄降级成"什么都能干"）。

三条安全边界：

1. **默认只读**：白名单默认 ``read_file`` / ``list_files``；子代理不能写文件、跑命令；
2. **不能自我批准**：白名单里即使出现敏感工具，调度器也不会替它走审批——敏感工具在
   子代理语境下一律拒绝（父会话的审批记录不能被子代理"借用"）；
3. **深度有限**：``depth`` 从 1 开始，超过 ``max_depth`` 直接拒绝，防止子代理递归炸开。
"""

from dataclasses import dataclass
from typing import Sequence

from app.common.exception import SubagentError

DEFAULT_ALLOWED_TOOLS = frozenset({"read_file", "list_files"})


@dataclass(frozen=True)
class DelegationPolicy:
    allowed_tools: frozenset[str] = DEFAULT_ALLOWED_TOOLS
    max_depth: int = 2
    max_concurrency: int = 2
    max_iterations: int = 8
    token_budget: int = 8000

    def __post_init__(self) -> None:
        if not self.allowed_tools:
            raise ValueError("allowed_tools must not be empty")
        for name in ("max_depth", "max_concurrency", "max_iterations", "token_budget"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")

    def check_depth(self, depth: int) -> None:
        if depth < 1:
            raise SubagentError("subagent depth must start at 1", detail={"depth": depth})
        if depth > self.max_depth:
            raise SubagentError(
                f"subagent depth {depth} exceeds max_depth {self.max_depth}",
                detail={"depth": depth, "max_depth": self.max_depth},
            )

    def resolve_tools(
        self, requested: Sequence[str], available: Sequence[str]
    ) -> tuple[str, ...]:
        """把「任务请求的工具」与「策略允许的工具」与「注册表实际有的工具」求交。"""
        wanted = set(requested) if requested else set(self.allowed_tools)
        forbidden = wanted - set(self.allowed_tools)
        if forbidden:
            raise SubagentError(
                f"tool(s) not permitted for subagents: {sorted(forbidden)}",
                detail={"forbidden": sorted(forbidden), "allowed": sorted(self.allowed_tools)},
            )
        available_set = set(available)
        missing = wanted - available_set
        if missing:
            raise SubagentError(
                f"requested tool(s) not registered: {sorted(missing)}",
                detail={"missing": sorted(missing)},
            )
        selected = tuple(name for name in available if name in wanted)
        if not selected:
            raise SubagentError("no permitted tools are available for this subagent")
        return selected

    def iter_limit(self, requested: int) -> int:
        return min(requested, self.max_iterations)

    def budget_exceeded(self, tokens: int) -> bool:
        return tokens > self.token_budget
