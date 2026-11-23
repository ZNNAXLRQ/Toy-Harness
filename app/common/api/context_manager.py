"""上下文管理端口：把会话历史压到 token 预算内，同时保住前缀缓存。

**唯一的硬约束：只能裁 ``messages`` 的尾部，永远不动 ``prefix``。**
prefix 一旦被裁，供应商的 prefix cache 每轮都失效——两个「高级功能」会互相
拆台。``prepare`` 因此把两者分开返回，而不是返回一个混合列表。

摘要（summarizer）用来在裁剪前压掉中段历史；摘要结果必须写回会话（作为一条
``role="summary"`` 或系统注记），否则下一轮无法复现同一上下文。
"""

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class PreparedContext:
    """一次模型调用前的上下文快照。"""

    prefix: Sequence[Mapping[str, Any]]
    messages: Sequence[Mapping[str, Any]]
    estimated_tokens: int
    trimmed_messages: int = 0
    summarized: bool = False


class ContextManager(Protocol):
    async def prepare(
        self,
        session_id: str,
        prefix: Sequence[Mapping[str, Any]],
        messages: Sequence[Mapping[str, Any]],
        *,
        budget: int | None = None,
    ) -> PreparedContext:
        """按预算裁剪尾部并估算 token；prefix 原样返回。"""
        ...

    def record_usage(self, tokens: int) -> None:
        """记录一次真实用量（来自 provider 的 usage），用于后续估算校准。"""
        ...

    def should_compact(self) -> bool:
        """下一轮之前是否需要先做摘要压缩。"""
        ...
