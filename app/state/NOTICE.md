# C 开工必读（3 分钟）

本目录属于 State / Persistence。开工前先读：

1. `app/common/api/IMPLEMENTATION_NOTES.md` 第 1 节（完整清单与验收命令）。
2. `schema.sql` —— 已由 A 写全（含 `approvals` 表、`messages(session_id, seq)` 唯一索引）；
   这是唯一建表来源，字段语义看 `app/common/api/state_store.py` 的视图与注释。
3. `tests/contracts/conftest.py` —— 你的接入点（两处）：
   - `_sqlite_store()` → `StateStore` 实现（61 条契约）；
   - `_sqlite_subagent_store()` → `SubagentRunStore` 实现（5 条契约）。

## 容器期望的入口（写死在 `app/core/config/container.py`）

```python
# app/state/persistence/factory.py
def create_stores(settings) -> tuple[StateStore, SubagentRunStore]: ...

# app/state/context/factory.py
def create_context_manager(settings) -> ContextManager: ...

# app/state/agents/default_agents_md_loader.py（可选，无参构造）
class DefaultAgentsMdLoader: ...
```

`build_default_container()` 探测不到这些入口时，会把「缺哪个组件/谁负责/期望路径」
汇总成一条 `ConfigError`——照着报错补就行。

## 为什么新增了 approvals 表

审批要跨重启恢复（谁在等审批、谁批的、何时批的），原 7 张表没有一张能表达这个状态
（audit_logs 是流水，不表达状态）。这是对 Java 版 schema 的唯一偏离；表结构可微调，
但必须能表达：哪个 call、待审批/批准/拒绝、谁批的、何时批的、同一 call 唯一一条。

## 最容易实现错的点

1. `claim_tool_call` 必须是 `UPDATE ... WHERE status=:from` + `rowcount` 判定；
   目标为 approved 时还要 `EXISTS(approvals ... status='approved')`；
2. `record_approval_request` / `decide_approval` 的跨表写入必须各在一个事务里；
3. `finish_tool_call` 的四件事（observation、reversible、tool 消息、终态）同事务；
4. 终态不可迁出；`set_tool_status` 不允许写 awaiting/approved/dispatched；
5. `load_open_turn` 是恢复入口（会写库），只读查询用 `peek_open_turn`；
   `TurnState.open_call_ids` 必须填（loop 靠它恢复派发）；
6. `decide_approval` 写的审计行必须能被 `query_audits` 查到（契约测试验收点）；
7. `query_audits` 返回**最新** limit 条（升序，limit>=1）；`latest_message_seq` 供检查点；
8. `expire_pending_approvals` 同事务置 denied + 写审计；待审批列表过滤终态会话；
9. 回调/摘要：`ContextManager.build_summary` 只返回文本，由 loop 写回 `role=summary`。
