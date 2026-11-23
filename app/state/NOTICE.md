# C 开工必读（3 分钟）

本目录属于 State / Persistence。开工前先读：

1. `app/common/api/IMPLEMENTATION_NOTES.md` 第 1 节（你的完整清单与验收命令）。
2. `schema.sql` —— **已由 A 写全（含新增的 `approvals` 表）**，这是唯一的建表来源；
   表结构不要自己再发明，字段语义看 `app/common/api/state_store.py` 的视图与注释。
3. `tests/contracts/conftest.py` —— 你的接入点。把 `_sqlite_store()` 从 skip 换成
   真实实现构造，47 条用例即验收。

## 为什么新增了 approvals 表

审批要跨重启恢复（「谁在等审批、谁批的、何时批的」），原 7 张表没有任何一张能表达
这个语义（`audit_logs` 是流水，不表达状态）。本项目对 Java 版 schema 的唯一偏离，
已在 `schema.sql` 头部写明理由；表结构可以微调，但必须能表达：哪个 call、待审批/
批准/拒绝、谁批的、何时批的，以及同一 call 唯一一条审批。

## 最容易实现错的四个点

1. `claim_tool_call` 必须是 `UPDATE ... WHERE status=:from` + `rowcount` 判定，不许读-改-写；
2. `record_approval_request` / `decide_approval` 的跨表写入必须各在一个事务里；
3. `finish_tool_call` 的四件事（observation、reversible、tool 消息、终态）同事务；
4. 终态不可迁出；`load_open_turn` 是恢复入口（会写库），只读查询用 `peek_open_turn`。
