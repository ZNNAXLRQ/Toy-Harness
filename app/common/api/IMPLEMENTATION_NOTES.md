# 接口实现指南（给 B / C / D）

本文件是所有端口的「施工入口」。开工前请读对应小节，按顺序做三件事：
**读端口 docstring → 找到实现位置 → 跑契约测试变绿**。

如果实现时发现契约不够用：**不要在实现里绕开**，直接找 A 改契约并同步更新契约测试。
绕开的实现会在联调阶段变成三份互相矛盾的事实。

## 0. 共同约定

- 端口在 `app/common/api/`，**实现层互不 import**，只依赖端口。
- 落库实体一律用端口里的**结构化视图**（`SessionView`/`MessageView`/`ToolCallView`/
  `ApprovalView`/`AuditView`/`CheckpointView`/`SubagentRunView`）——你的 dataclass 只要
  字段同名就自动满足，不需要继承。
- 时间是 **int 毫秒**（数据库与事件统一），不要用 float 秒。
- 错误体系：业务失败返回结果对象；harness 故障抛 `HarnessError` 子类。
- 密钥/敏感信息：事件构造时已自动脱敏；审计/异常 detail 必须经
  `app.common.security.redact` 处理后再存储/返回。
- **不要 import `app/common/api/runtime.py` 和 `app/common/model/runtime.py`**：它们是
  nano-codex 原型的旧注入点，与端口存在同名类（`LLMProvider`/`ApprovalService`/
  `ModelResponse`/`ToolResult`），PR2 会整段删除。新代码只从 `app.common.api` 导入。

## 1. C：state / persistence（最优先，其他人都等你）

实现位置：`app/state/persistence/`（`db.py` + 各 repository + 一个满足 `StateStore`
的实现类）。schema 直接执行仓库根目录的 `schema.sql`（含新增的 `approvals` 表；
`messages(session_id, seq)` 已建唯一索引）。

必须实现的端口：

- `StateStore`：会话/消息/工具调用/观察/审批/审计/轮次投影/事务；
- `CheckpointStore`、`SubagentRunStore`；
- 验收：`tests/contracts` 的 59 条用例（见下）。

几个**必须按字面实现**的点（审阅时会被重点看）：

1. `claim_tool_call` 必须是**单条条件更新**：`UPDATE ... WHERE id=:id AND status=:from`，
   按 `rowcount == 1` 判定；**当目标状态是 `approved` 时必须同时要求 approvals 行
   `status='approved'`**（见方法的 SQL 注释）。实现步骤是**两步**：① 先查一次审批行，
   不存在/不是 approved → 抛 `ConflictError`（这是数据不一致，不是 CAS 竞争）；
   ② 再执行单条条件 UPDATE 完成迁移。不要退回读-改-写。
2. `record_approval_request` 必须**同事务**完成：插 `approvals` 行 + 把调用置
   `awaiting_approval`；按 `call_id` 唯一（重复调用返回已有记录）；校验会话一致、
   `tool_name/arguments` 与 tool_call 行一致、调用为 `pending`、会话非终态。
3. `decide_approval` 必须**同事务**完成：更新审批行 + 写 `audit_logs(action='approval_decided')`；
   不改 tool_calls 状态（消费由 CAS 完成）；已决策/调用已非 awaiting/会话终态均报错。
4. `record_assistant_turn` 一次写入 assistant 消息 + 全部 tool_calls；任何冲突都不得留下
   半条消息；会话终态时拒绝。
5. `finish_tool_call` 同事务写 observation（JSON 含 `status/result/receipt`）+ `reversible`
   + `role="tool"` 消息 + 终态；会话已 `failed/abandoned` 时抛 `ConflictError`。
6. `load_open_turn` 是**恢复入口**（会写库、判 UNKNOWN 幂等）；`peek_open_turn` 是只读投影
   （在飞调用放 `notes["in_flight"]`）。`TurnState.open_call_ids` 必须填，loop 靠它恢复派发。
7. `record_audit` / `query_audits` 是审计的唯一写入/读取通道；`query_audits` 返回
   **最新的** `limit` 条（按 `created_at` 升序，`limit>=1`，否则 `StateError`）；
   `decide_approval` 的审计行必须能被 `query_audits` 查到（契约测试的验收点）。
8. `latest_message_seq` 供检查点打点（回滚补偿集合按它计算）。
9. `expire_pending_approvals`：放弃一轮时把该会话 pending 审批置 denied 并写
   `approval_expired` 审计；`list_pending_approvals` 不返回终态会话的遗留审批。
10. 终态不可迁出：`failed/abandoned` 会话上拒绝 update/append/record/claim/approval 写入。

验收（改一处即可）：

```text
tests/contracts/conftest.py 里 _sqlite_store()：把 skip 换成你的实现构造
uv run pytest tests/contracts -q     # 59 条 memory + sqlite 全绿
```

> 注意：现在 skip 让 CI 常绿。**交付当天必须改 conftest**，否则等于没验收。

## 2. B：tools / approval / audit / rollback

实现位置：`app/tools/`。

- `Tool`：`async execute(arguments) -> ToolResult`；内部可同步，由 registry 用
  `asyncio.to_thread` 包一次；写操作成功时返回 `receipt`（补偿凭据）。
- `ToolRegistry`：只执行不审批；`register` 同名必须抛 `ToolError`。
- `ApprovalService`：只做策略 + 四个委托方法（`record/decide/pending/find_for_call`
  全部委托 `StateStore`）。**不要自己开事务或连库**。
- `AuditService`：`log` = **先对 Mapping 的值脱敏（`redact_values`）再 JSON 序列化**，
  然后委托 `StateStore.record_audit`；顺序反了会漏掉带引号/反斜杠的密钥。
  `query` 委托 `query_audits`。**审批决策的审计由 store 同事务写，不要重复记录。**
  参考实现见 `tests/fakes/memory_audit_service.py`（B 的实现照它写即可）。
- 回滚：`CheckpointStore` 由 C 实现。补偿集合按**消息序号**算（不是 created_at）：
  `messages` 中 `seq > checkpoint.seq` 且 `role='tool'` 的 `tool_call_id`，再过滤
  `reversible` 为真，逆序执行 `Tool.rollback(get_receipt(call_id))`；
  `create_checkpoint` 的 seq 取 `StateStore.latest_message_seq`，loop 在每次写操作派发前调用。

## 3. D：web / SSE / 前端

实现位置：`app/web/`、`app/main.py`。

- 审批：`GET /api/sessions/{id}/approvals?pending=1` → `POST /api/sessions/{id}/approvals/{approval_id}`
  （decide）→ 紧接着 `AgentLoop.resume_turn(session_id)`（不传 approval_id 时由 loop 按
  call 找回已决策记录）。decide 本身不派发工具。
  **`load_open_turn` 返回 WAITING 且 `pending_approval_id` 非空 = 已决策、待恢复执行**，
  页面应显示「恢复中/正在执行决定」，不要显示一个已经空了的审批队列。
- 放弃一轮（abandon）后待审批会被 `expire_pending_approvals` 置为 denied，
  重连快照里不该再出现它们。
- 断线重连的权威快照（不是只拉 messages）：会话状态 / messages / tool-calls /
  pending approvals。
- **只读接口不要调用 `load_open_turn`**（它会把在飞调用判 UNKNOWN 并写库）；用
  `peek_open_turn`。
- SSE 事件：`AgentEvent` 构造时校验 + 脱敏，直接 `event.to_sse()`；推送失败只记日志。

## 4. A：core（loop / provider / subagent / container）

- `AgentLoop.run_turn`：开头同事务写 `sessions.status=OPEN`；每轮 `provider.complete`；
  `record_assistant_turn`；逐个调用 `approval.requires_approval` → `request` → 返回
  `waiting_approval`，或 `claim_tool_call(pending→dispatched)` → 执行 → `finish_tool_call`。
- `resume_turn`：按 `TurnState.open_call_ids` 恢复：已决策审批 → `claim(awaiting→approved)`
  → `claim(approved→dispatched)` → 执行；被拒绝则写 `denied` 并结束本轮。
- 同一会话加锁，保证单写者；`load_open_turn` 只在启动恢复与 turn 入口调用。
- 前缀：`CompletionRequest.prefix` 每轮逐字节一致，不裁 prefix、不把时间戳塞进去。
- 每轮把真实用量交给 `ContextManager.record_usage`，摘要用 `build_summary` + 写回
  `role=summary` 消息。
