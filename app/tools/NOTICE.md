# B 开工必读（3 分钟）

本目录属于 Tools / Approval / Audit / Rollback。开工前先读：

1. `app/common/api/IMPLEMENTATION_NOTES.md` 第 2 节（完整清单）。
2. `schema.sql` 新增了 `approvals` 表；审批与审计的**写入/读取都由 C 的 `StateStore`
   提供方法**，你只写委托，不要自己开事务或连库：
   - 审批：`record_approval_request` / `decide_approval` / `list_pending_approvals` /
     `get_approval_for_call`
   - 审计：`record_audit` / `query_audits`（审批决策的审计由 decide_approval 同事务写入，
     `AuditService.log` 不要重复记录）

## 你要交付的四个端口

- `Tool`：`async execute`、成功时返回 `receipt`（补偿凭据）、`rollback(receipt)`；
- `ToolRegistry`：只执行不审批；同名注册必须抛 `ToolError`；
- `ApprovalService`：`requires_approval` 策略 + 四个委托方法；
- `AuditService`：序列化 + `redact` 后委托 `record_audit`；`query` 委托 `query_audits`。

## 两个最容易跑偏的点

- `ToolResult.status="denied"` 只能由 loop 产生（审批拒绝或同批跳过），工具实现永远返回
  `success`/`error`；
- 回滚补偿集合按**消息序号**算：检查点 seq 之后落库的 `role='tool'` 消息对应的调用，
  再过滤 `reversible`；**不要用 `created_at`**（工具调用的时间戳早于检查点，会得到空集）。
