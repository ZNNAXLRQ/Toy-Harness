# C 开工必读（3 分钟）

本目录属于 State / Persistence。开工前先读：

1. `app/common/api/IMPLEMENTATION_NOTES.md` 第 1 节（完整清单与验收命令）。
2. `schema.sql` —— 已由 A 写全（含新增的 `approvals` 表、`messages(session_id, seq)` 唯一索引）；
   这是唯一建表来源，字段语义看 `app/common/api/state_store.py` 的视图与注释。
3. `tests/contracts/conftest.py` —— 你的接入点：把 `_sqlite_store()` 从 skip 换成实现构造，
   59 条用例红转绿即验收。

## 为什么新增了 approvals 表

审批要跨重启恢复（谁在等审批、谁批的、何时批的），原 7 张表没有一张能表达这个状态
（audit_logs 是流水，不表达状态）。这是对 Java 版 schema 的唯一偏离；表结构可微调，
但必须能表达：哪个 call、待审批/批准/拒绝、谁批的、何时批的、同一 call 唯一一条。

## 最容易实现错的六个点

1. `claim_tool_call` 必须是 `UPDATE ... WHERE status=:from` + `rowcount` 判定；
   **目标为 approved 时还要 `EXISTS(approvals WHERE call_id=? AND status='approved')`**；
2. `record_approval_request` / `decide_approval` 的跨表写入必须各在一个事务里；
3. `finish_tool_call` 的四件事（observation、reversible、tool 消息、终态）同事务；
4. 终态不可迁出；`set_tool_status` 不允许写 awaiting/approved/dispatched；
5. `load_open_turn` 是恢复入口（会写库），只读查询用 `peek_open_turn`；
   `TurnState.open_call_ids` 必须填（loop 靠它恢复派发）；
6. `decide_approval` 写的审计行必须能被 `query_audits` 查到（契约测试验收点）；
   `query_audits` 返回**最新** limit 条（升序，limit>=1），不是最早；
7. `expire_pending_approvals` 在放弃轮次时把 pending 审批置 denied 并写
   `approval_expired` 审计；`list_pending_approvals` 不返回终态会话的遗留审批。
