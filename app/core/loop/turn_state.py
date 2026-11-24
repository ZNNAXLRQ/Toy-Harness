"""单轮状态机：一次 run 期间在内存里累加的计数。

持久化的**轮次投影**在 ``StateStore.load_open_turn``（哪个调用还没终态、是否在等
审批）；这里只放「本进程本次 run 见过什么」——迭代次数、累计 token、是否已经因为
拒绝/超限而决定终止。

把两者分开是有意的：投影可以从数据库重建，进度不能，也不该假装能（schema 没有
token 列）。恢复后 ``iteration`` 由 ``TurnState`` 重新数出来，token 从 0 开始。
"""

from dataclasses import dataclass, field

from app.common.api.llm_provider import ModelResponse
from app.common.api.state_store import ToolCallStatus


@dataclass
class TurnProgress:
    """一次 run 的进程内进度。"""

    session_id: str
    iteration: int = 0
    tokens: int = 0
    executed_calls: list[str] = field(default_factory=list)
    last_content: str | None = None

    def record_response(self, response: ModelResponse) -> None:
        self.iteration += 1
        self.tokens += response.tokens
        if response.content:
            self.last_content = response.content


def is_open_status(status: ToolCallStatus | str) -> bool:
    """调用是否还没终态（用于判断本轮是否还要继续执行）。"""
    try:
        return ToolCallStatus(status) not in {
            ToolCallStatus.COMPLETED,
            ToolCallStatus.FAILED,
            ToolCallStatus.DENIED,
            ToolCallStatus.UNKNOWN,
        }
    except ValueError:
        return False
