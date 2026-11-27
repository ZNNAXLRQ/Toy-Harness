# B 开工必读（3 分钟）

本目录属于 Tools / Approval / Audit / Rollback。开工前先读：

1. `app/common/api/IMPLEMENTATION_NOTES.md` 第 2 节（完整清单）。
2. `schema.sql` 新增了 `approvals` 表；审批与审计的**写入/读取都由 C 的 `StateStore`
   提供方法**，你只写委托，不要自己开事务或连库：
   - 审批：`record_approval_request` / `decide_approval` / `list_pending_approvals` /
     `get_approval_for_call`
   - 审计：`record_audit` / `query_audits`（审批决策的审计由 decide_approval 同事务写入，
     `AuditService.log` 不要重复记录）

## 迁移提醒（PR2 之后最重要的一条）

A 的 loop 已经改成只认 `common/api` 的**异步**端口。现有 `app/tools/registry.py` 是
原型实现（同步、依赖 `app/common/api/runtime.py`），**loop 跑不到它**。你开工时的第一
件事：把 `ToolRegistry` 实现成异步端口（`async execute` / `get` / `schemas`），
工具函数保持同步、在**注册表内部**用 `asyncio.to_thread` 包一次；完成后删除
`app/common/api/runtime.py` 与 `app/common/model/runtime.py`。参考替身：
`tests/fakes/fake_tools.py`。

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

## 容器按这些名字探测你的实现（`app/core/config/container.py`）

```python
from app.tools.registry import create_async_builtin_registry   # (workspace) -> ToolRegistry
from app.tools.approval import DefaultApprovalService          # (state) -> ApprovalService
from app.tools.audit import DefaultAuditService                # (state) -> AuditService
```

你已经提供了这三个（`feat/web-sse` 上），容器会直接装配；**保持这些名字/签名不变**即可。
迁移完成后删除 `app/common/api/runtime.py` 与 `app/common/model/runtime.py`（现在还被旧
`ToolRegistry` / `create_builtin_registry` 引用）。
