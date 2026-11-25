"""上下文管理端口：把会话历史压到 token 预算内，同时保住前缀缓存。

**唯一的硬约束：只能裁 ``messages`` 的尾部，永远不动 ``prefix``。**
prefix 一旦被裁，供应商的 prefix cache 每轮都失效——两个「高级功能」会互相
拆台。``prepare`` 因此把两者分开返回，而不是返回一个混合列表。

## 摘要写回（不要再让实现私接 StateStore）

早期版本写「摘要结果必须写回会话」，但没有给通道，实现者只能自己偷偷连库。
现在明确：摘要由 ``build_summary`` **生成并返回文本**，由 loop 负责
``append_message(role=SUMMARY, ...)``。这样写入顺序、事务边界、恢复规则都只有一个
负责人（loop），ContextManager 保持纯计算。

摘要消息是压缩注记，不构成一轮的开始/结束：恢复推导会跳过尾部的 summary/system
再判断轮次状态（见 ``state_store`` 的规则）。
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
    needs_summary: bool = False


class ContextManager(Protocol):
    async def prepare(
        self,
        session_id: str,
        prefix: Sequence[Mapping[str, Any]],
        messages: Sequence[Mapping[str, Any]],
        *,
        budget: int | None = None,
    ) -> PreparedContext:
        """按预算裁剪尾部并估算 token；prefix 原样返回，不裁不改。"""
        ...

    async def build_summary(
        self, session_id: str, messages: Sequence[Mapping[str, Any]]
    ) -> str:
        """生成中段历史的摘要文本；**不写库**，由 loop 以 role=summary 追加。"""
        ...

    def record_usage(self, tokens: int) -> None:
        """记录一次真实用量（来自 provider 的 usage），用于后续估算校准。"""
        ...

    def should_compact(self) -> bool:
        """下一轮之前是否需要先做摘要压缩。"""
        ...
