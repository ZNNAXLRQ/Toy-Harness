"""回滚端口：检查点 + 按逆序执行补偿动作。

范围要说清楚（答辩时会被问）：

- 只能回滚**本 harness 自己产生的、有补偿凭据的**本地写操作；
- 远程副作用、已经结束的进程、被外部修改过的文件，**不承诺恢复**；
- 回滚本身也写审计，并记录哪些动作失败，不假装全部成功。

## 补偿集合怎么算（本轮修正，之前按 created_at 算会得到空集）

工具调用的 ``created_at`` 在 **assistant 消息落库时**就写了，早于「写操作之前打的
检查点」，所以按时间戳过滤会把真正要回滚的调用排除掉。正确算法按**消息序号**：

1. `create_checkpoint` 取当前 `StateStore.latest_message_seq(session_id)` 作为
   `checkpoints.seq`（打点即"此刻之前的历史不再补偿"）；
2. 需要补偿的调用 = **`messages` 中 `seq > checkpoint.seq` 且 `role='tool'` 的消息**
   对应的 `tool_call_id`（即检查点之后才落库的观察结果），再过滤
   `tool_calls.reversible` 为真；
3. 每个调用的凭据由 `StateStore.get_receipt(call_id)` 读回；
4. 按消息 seq 逆序执行 `Tool.rollback(receipt)`：部分失败要如实返回，已执行的补偿不回退。

**调用时机**：loop 在**每次派发写操作工具之前**调用 `create_checkpoint`；读操作不需要。
`state_json` 保存打点时的现场快照（消息条数/任务状态），由调用方决定内容。
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
        """在当前消息序号上打点；**loop 在每次写操作派发前调用**。

        ``seq`` 必须取 ``StateStore.latest_message_seq(session_id)``，
        不能取 ``created_at``（工具调用的时间戳早于检查点，会在补偿时被排除）。
        """
        ...

    async def list_checkpoints(self, session_id: str) -> Sequence[CheckpointView]: ...

    async def rollback(self, session_id: str, checkpoint_id: str) -> RollbackResult:
        """按逆序执行该检查点之后的补偿动作；不删除历史消息，只追加回滚记录。"""
        ...
