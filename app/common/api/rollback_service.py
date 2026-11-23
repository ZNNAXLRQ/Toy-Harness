"""回滚端口：检查点 + 按逆序执行补偿动作。

范围要说清楚（答辩时会被问）：

- 只能回滚**本 harness 自己产生的、有补偿凭据的**本地写操作；
- 远程副作用、已经结束的进程、被外部修改过的文件，**不承诺恢复**；
- 回滚本身也写审计，并记录哪些动作失败，不假装全部成功。

## 补偿集合怎么算（本轮补的定义）

1. 检查点在 `checkpoints.seq` 上打点（= 当时的 messages.seq）；
2. 需要补偿的调用 = 该会话中 **created_at 落在检查点之后、且 `reversible` 为真** 的
   tool_calls（`StateStore.list_tool_calls` 按创建顺序返回，便于逆序执行）；
3. 每个调用的凭据由 `StateStore.get_receipt(call_id)` 读回；
4. 逆序执行 `Tool.rollback(receipt)`：部分失败要如实返回，已执行的补偿不回退。

`checkpoints.state_json` 用于保存打点时的会话快照（例如消息条数/任务状态），
由调用方决定内容；schema 里已有该列，本端口必须暴露它，否则回滚后无法重建现场。
"""

from dataclasses import dataclass, field
from typing import Protocol, Sequence


class CheckpointView(Protocol):
    """``checkpoints`` 表的一行（C 的 dataclass 自动满足）。"""

    id: str
    session_id: str
    seq: int
    state_json: str | None
    description: str | None
    created_at: int


@dataclass(frozen=True)
class RollbackResult:
    checkpoint: CheckpointView
    reverted: Sequence[str] = ()
    failed: Sequence[tuple[str, str]] = field(default_factory=tuple)
    """(call_id, 失败原因)。部分失败时 ``reverted`` 仍然有效，不要整批回退。"""

    @property
    def ok(self) -> bool:
        return not self.failed


class CheckpointStore(Protocol):
    """检查点持久化端口（C 实现，B 的 ``RollbackService`` 消费）。

    sample 把它放在 ``app/state/persistence/checkpoint_repository.py``；按依赖规则
    ``tools/rollback`` 不能直接 import 实现，因此需要这层端口。没有它，
    ``RollbackService`` 就只能自己在内存里记检查点——重启即失效。
    """

    async def save_checkpoint(
        self,
        session_id: str,
        *,
        seq: int,
        state_json: str | None = None,
        description: str | None = None,
    ) -> CheckpointView: ...

    async def list_checkpoints(self, session_id: str) -> Sequence[CheckpointView]: ...

    async def load_checkpoint(self, checkpoint_id: str) -> CheckpointView | None: ...


class RollbackService(Protocol):
    async def create_checkpoint(
        self, session_id: str, description: str | None = None
    ) -> CheckpointView:
        """在当前消息序号上打点；写操作之前调用。"""
        ...

    async def list_checkpoints(self, session_id: str) -> Sequence[CheckpointView]: ...

    async def rollback(self, session_id: str, checkpoint_id: str) -> RollbackResult:
        """按逆序执行该检查点之后的补偿动作；不删除历史消息，只追加回滚记录。"""
        ...
