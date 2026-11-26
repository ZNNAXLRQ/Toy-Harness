"""委派策略：工具白名单、深度上限、并发上限、token/迭代预算。

策略是**纯数据 + 校验**，不含 I/O；调度器在执行前后调用它，违规一律抛
``SubagentError``（失败要响亮，而不是让子代理悄悄降级成"什么都能干"）。

三条安全边界：

1. **子代理只读是硬编码的**：只有 ``READ_ONLY_TOOLS`` 里的工具能在策略里出现；
   ``write_file`` / ``execute_command`` / ``run_python`` 这类工具**不能**通过配置
   ``allowed_tools`` 打开。子代理没有审批通道，放开写工具就等于"无审批写入"。
2. **不能自我批准**：将来若要允许敏感工具，必须给调度器注入真正的
   ``ApprovalService``、为子代理的每次调用新建审批记录，并且禁止子代理消费
   父会话的批准（审批是按 call 记录的，不能跨调用复用）。
3. **深度有限**：``depth`` 从 1 开始，超过 ``max_depth`` 直接拒绝，防止递归炸开。
"""

from dataclasses import dataclass
from typing import Sequence

from app.common.exception import SubagentError

READ_ONLY_TOOLS = frozenset({"read_file", "list_files"})
DEFAULT_ALLOWED_TOOLS = READ_ONLY_TOOLS


@dataclass(frozen=True)
class DelegationPolicy:
    allowed_tools: frozenset[str] = DEFAULT_ALLOWED_TOOLS
    max_depth: int = 2
    max_concurrency: int = 2
    max_iterations: int = 8
    token_budget: int = 8000

    def __post_init__(self) -> None:
        if not self.allowed_tools:
            raise SubagentError("subagent allowed_tools must not be empty")
        writable = set(self.allowed_tools) - READ_ONLY_TOOLS
        if writable:
            raise SubagentError(
                "subagents are read-only; these tools cannot be enabled: "
                f"{sorted(writable)}",
                detail={"requested": sorted(self.allowed_tools), "read_only": sorted(READ_ONLY_TOOLS)},
            )
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
        """把「任务请求的工具」与「策略允许的工具」与「注册表实际有的工具」求交。

        任务**没有显式请求**工具时，取「策略允许 ∩ 注册表可用」——不能因为注册表缺
        ``list_files`` 就让一个只需要 ``read_file`` 的子任务失败；显式请求时才要求
        请求的工具必须全部在策略内且已注册。
        """
        available_set = set(available)
        if requested:
            wanted = set(requested)
            forbidden = wanted - set(self.allowed_tools)
            if forbidden:
                raise SubagentError(
                    f"tool(s) not permitted for subagents: {sorted(forbidden)}",
                    detail={"forbidden": sorted(forbidden), "allowed": sorted(self.allowed_tools)},
                )
            missing = wanted - available_set
            if missing:
                raise SubagentError(
                    f"requested tool(s) not registered: {sorted(missing)}",
                    detail={"missing": sorted(missing)},
                )
        else:
            wanted = set(self.allowed_tools) & available_set
            if not wanted:
                raise SubagentError(
                    "no permitted tools are available for this subagent",
                    detail={"allowed": sorted(self.allowed_tools), "available": sorted(available_set)},
                )
        selected = tuple(name for name in available if name in wanted)
        if not selected:
            raise SubagentError("no permitted tools are available for this subagent")
        return selected

    def iter_limit(self, requested: int) -> int:
        return min(requested, self.max_iterations)

    def budget_exceeded(self, tokens: int) -> bool:
        return tokens > self.token_budget
