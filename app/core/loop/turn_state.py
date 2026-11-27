"""单轮状态机：一次 run 期间在内存里累加的计数。

持久化的**轮次投影**在 ``StateStore.load_open_turn``（哪个调用还没终态、是否在等
审批）；这里只放「本进程本次 run 见过什么」——迭代次数、累计 token。

``iteration`` 在恢复时用 ``TurnState.iteration`` 初始化（整轮预算不能因为挂起/重启
被重置）；``tokens`` 无法从 schema 重建，恢复后从 0 开始（见 state_store 的说明）。
"""

from dataclasses import dataclass

from app.common.api.llm_provider import ModelResponse


@dataclass
class TurnProgress:
    """一次 run 的进程内进度。"""

    session_id: str
    iteration: int = 0
    tokens: int = 0

    def record_response(self, response: ModelResponse) -> None:
        self.iteration += 1
        self.tokens += response.tokens
