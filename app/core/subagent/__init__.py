"""子代理：受限并发/深度/预算的执行器 + 独立上下文 + 结果回流。

边界：子代理不落主会话的消息表，只落 ``subagent_runs``；工具默认只读；敏感工具在
子代理语境下一律拒绝（父会话的审批不能被子代理借用）。
"""

from app.core.subagent.default_subagent_scheduler import DefaultSubagentScheduler
from app.core.subagent.delegation_policy import DEFAULT_ALLOWED_TOOLS, DelegationPolicy
from app.core.subagent.subagent_context import SubagentContext
from app.core.subagent.subagent_result_merger import merge_subagent_result, result_json

__all__ = [
    "DEFAULT_ALLOWED_TOOLS",
    "DefaultSubagentScheduler",
    "DelegationPolicy",
    "SubagentContext",
    "merge_subagent_result",
    "result_json",
]
