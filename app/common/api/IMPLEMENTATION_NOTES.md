# 接口实现指南（给 B / C / D）

本文件是所有端口的「施工入口」。开工前请读对应小节，按顺序做三件事：
**读端口 docstring → 找到实现位置 → 跑契约测试变绿**。

如果实现时发现契约不够用：**不要在实现里绕开**，直接找 A 改契约并同步更新契约测试。
绕开的实现会在联调阶段变成三份互相矛盾的事实。

## 0. 共同约定

- 端口在 `app/common/api/`，**实现层互不 import**，只依赖端口。
- 落库实体一律用端口里的**结构化视图**（`SessionView`/`MessageView`/`ToolCallView`/
  `ApprovalView`/`CheckpointView`/`SubagentRunView`）——你的 dataclass 只要字段同名，
  就自动满足，不需要继承。
- 时间是 **int 毫秒**（数据库与事件统一），不要用 float 秒。
- 错误体系：业务失败返回结果对象；harness 故障抛 `HarnessError` 子类。
- 密钥/敏感信息：事件构造时已自动脱敏；日志里用 `app.common.security.redact`。

## 1. C：state / persistence（最优先，其他人都等你）

实现位置：`app/state/persistence/`（`db.py` + 各 repository + 一个满足 `StateStore`
的实现类）。schema 直接执行仓库根目录的 `schema.sql`（**已包含新增的 `approvals` 表**）。

必须实现的端口：

- `StateStore`（`app/common/api/state_store.py`）：会话/消息/工具调用/观察/审批/轮次投影/事务；
- `CheckpointStore`（`rollback_service.py`）、`SubagentRunStore`（`subagent_scheduler.py`）；
- 验收：`tests/contracts` 的 47 条用例（见下）。

几个**必须按字面实现**的点（审阅时会被重点看）：

1. `claim_tool_call` 必须是**单条条件更新**：`UPDATE tool_calls SET status=:to WHERE id=:id
   AND status=:from`，按 `rowcount == 1` 判定。读-改-写会破坏并发恢复的单飞保证。
2. `record_approval_request` 必须**同事务**完成：插 `approvals` 行 + 把调用置
   `awaiting_approval`；按 `call_id` 唯一（重复调用返回已有记录）。
3. `decide_approval` 必须**同事务**完成：更新审批行 + 写 `audit_logs(action='approval_decided')`；
   **不改 tool_calls 状态**（消费由 `claim_tool_call(awaiting→approved)` 单独完成）。
4. `record_assistant_turn` 一次写入 assistant 消息 + 全部 tool_calls；任何冲突都不得留下
   半条消息。
5. `finish_tool_call` 同事务写 observation（JSON 含 `status/result/receipt`）+ `reversible`
   + `role="tool"` 消息 + 终态；会话已 `failed/abandoned` 时抛 `ConflictError`。
6. `load_open_turn` 是**恢复入口**，是唯一允许写的地方（判 UNKNOWN 时落 `failed`，幂等）；
   另实现只读的 `peek_open_turn`（不写任何东西，`dispatched` 无观察结果放进
   `notes["in_flight"]`）。
7. 终态不可迁出：`update_session` 在 `failed/abandoned` 会话上必须抛 `StateError`；
   `set_tool_status` 不允许写 `approved`/`dispatched`（必须走 CAS）。

验收（改一处即可）：

```text
tests/contracts/conftest.py 里 _sqlite_store()：把 skip 换成你的实现构造
uv run pytest tests/contracts -q     # 47 条 memory + sqlite 全绿
```

> 注意：现在 skip 让 CI 常绿。**交付当天必须改 conftest**，否则等于没验收。

## 2. B：tools / approval / audit / rollback

实现位置：`app/tools/`。

- `Tool`（`app/common/api/tool.py`）：`async execute(arguments) -> ToolResult`；
  内部实现可以同步，由 registry 用 `asyncio.to_thread` 包一次；写操作成功时返回
  `receipt`（补偿凭据，如备份路径）。
- `ToolRegistry`（`tool_registry.py`）：`execute` **只执行、不审批、不返回 denied**；
  `register` 同名必须抛 `ToolError`（运行期工具身份不变）。
- `ApprovalService`（`approval_service.py`）：只做两件事——
  1. `requires_approval(tool_name, arguments)` 的策略（放 `permission_policy.py`）；
  2. `request/decide/pending/find_for_call` **全部委托**给 `StateStore` 的
     `record_approval_request/decide_approval/list_pending_approvals/get_approval_for_call`。
     **不要自己开事务、不要自己连库**——审批的原子性由 C 的 store 保证。
- `AuditService`（`audit_service.py`）：写 `audit_logs`。注意审批决策的审计由
  `StateStore.decide_approval` 顺带写入，避免跨组件事务；`AuditService.log` 用于其它动作。
- 回滚（`rollback_service.py`）：`CheckpointStore` 由 C 实现；补偿集合算法见该文件
  docstring（检查点之后、`reversible=1` 的调用，逆序执行，凭据用
  `StateStore.get_receipt` 读回）。

## 3. D：web / SSE / 前端

实现位置：`app/web/`、`app/main.py`。

- 审批流程：`POST /api/approvals/{id}` → `ApprovalService.decide()` → 再调
  `AgentLoop.resume_turn(session_id)`（不传 approval_id 时由 loop 按 call 找回已决策记录）。
  **decide 不派发工具**，派发由 resume 用 CAS 抢占完成。
- 断线重连的权威快照（不是只拉 messages）：
  1. `GET /api/sessions/{id}` → `sessions.status`
  2. `GET /api/sessions/{id}/messages`
  3. `GET /api/sessions/{id}/tool-calls`
  4. `GET /api/sessions/{id}/approvals?pending=1`
- **只读接口不要调用 `load_open_turn`**（它会把在飞的调用判 UNKNOWN 并写库）；用
  `peek_open_turn`。
- SSE 事件：`AgentEvent` 构造时自动脱敏 + 校验 payload 白名单；直接 `event.to_sse()`。
  事件是视图不是真相，推送失败只记日志，不能让 turn 失败。

## 4. A：core（loop / provider / subagent / container）

- `AgentLoop.run_turn`：开头同事务写 `sessions.status=OPEN`；每轮 `provider.complete`；
  `record_assistant_turn`；逐个调用 `approval.requires_approval` → `request` → 返回
  `waiting_approval`，或 `claim_tool_call(pending→dispatched)` → 执行 → `finish_tool_call`。
- `resume_turn`：找回已决策审批 → `claim_turn(awaiting→approved)` → `claim(approved→dispatched)`
  → 执行；被拒绝则写 `denied` 并结束本轮。
- 同一会话加锁，保证单写者；`load_open_turn` 只在启动恢复与 turn 入口调用。
- 前缀：`CompletionRequest.prefix` 每轮逐字节一致，不裁 prefix、不把时间戳塞进去。
