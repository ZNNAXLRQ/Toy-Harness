# B 开工必读（3 分钟）

本目录属于 Tools / Approval / Audit / Rollback。开工前先读：

1. `app/common/api/IMPLEMENTATION_NOTES.md` 第 2 节（你的完整清单）。
2. `schema.sql` 新增了 `approvals` 表；审批写入由 C 的 `StateStore` 提供方法
   （`record_approval_request` / `decide_approval` / `list_pending_approvals` /
   `get_approval_for_call`），**你只写委托，不要自己开事务或连库**。
3. `app/common/api/approval_service.py` 的模块说明写清了责任划分与原子性。

## 你要交付的四个端口

- `Tool`：`async execute`、成功时返回 `receipt`（补偿凭据）、`rollback(receipt)`；
- `ToolRegistry`：只执行不审批；同名注册必须抛 `ToolError`；
- `ApprovalService`：`requires_approval` 策略 + 四个委托方法；
- `AuditService`：写 `audit_logs`；审批决策的审计由 store 同事务写入，不要重复写。

## 两个最容易跑偏的点

- `ToolResult.status="denied"` **只能由 loop 产生**（审批拒绝或同批跳过），工具实现
  永远返回 `success`/`error`；
- 工具参数是模型给的原始 JSON 文本，先解析再按 JSON Schema 子集校验，不要假设它合法。
