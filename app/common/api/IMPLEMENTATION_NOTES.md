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
  nano-codex 原型的旧注入点，与端口存在同名类，等 B 的旧 registry 迁移完就删。
- **装配统一走 `app/core/config/container.py`**：D 的 `app/main.py` 用
  `build_default_container()`；它按固定入口探测实现，缺谁会把人和路径点名报出来。

## 1. C：state / persistence（最优先，其他人都等你）

实现位置：`app/state/persistence/`。schema 直接执行仓库根目录的 `schema.sql`
（含新增的 `approvals` 表、`messages(session_id, seq)` 唯一索引）。

**容器期望的入口（写死在这里，请照此提供）**：

```python
# app/state/persistence/factory.py
def create_stores(settings) -> tuple[StateStore, SubagentRunStore]: ...

# app/state/context/factory.py
def create_context_manager(settings) -> ContextManager: ...

# app/state/agents/default_agents_md_loader.py（可选）
class DefaultAgentsMdLoader: ...  # 无参构造
```

必须实现的端口：

- `StateStore`：会话/消息/工具调用/观察/审批/审计/轮次投影/事务；
- `ContextManager`：裁剪/摘要/用量（**只裁尾部，prefix 不动**）；
- `SubagentRunStore`：`subagent_runs` 的 save/update/load/list（**同一 run_id 重复 save
  抛 `StateError`，update 不存在抛 `NotFoundError`，list 按 created_at 升序**）。

验收（各改一处即可）：

```text
tests/contracts/conftest.py 里 _sqlite_store() / _sqlite_subagent_store()：
  把 skip 换成你的实现构造
uv run pytest tests/contracts -q
# StateStore 61 条 + SubagentRunStore 5 条，memory + sqlite 各一遍
```

几个**必须按字面实现**的点：

1. `claim_tool_call` 必须是单条条件 UPDATE（`rowcount == 1` 判定）；目标为 `approved`
   时还要 `EXISTS(approvals WHERE call_id=? AND status='approved')`。实现步骤是两步：
   先查审批（非 approved → `ConflictError`），再单条 UPDATE 做 CAS；不要读-改-写。
2. `record_approval_request` 同事务：插审批行 + 调用置 `awaiting_approval`；按 call 唯一。
3. `decide_approval` 同事务：更新审批行 + 写 `audit_logs(action='approval_decided')`；
   不改 tool_calls 状态（消费由 CAS 完成）。
4. `record_assistant_turn` 一次写 assistant 消息 + 全部 tool_calls，冲突不留半条消息。
5. `finish_tool_call` 同事务写 observation/reversible/tool 消息/终态；会话终态拒绝。
6. `load_open_turn` 是恢复入口（会写库、判 UNKNOWN 幂等）；`peek_open_turn` 只读；
   `TurnState.open_call_ids` 必须填。
7. `record_audit`/`query_audits` 是审计唯一通道；`query_audits` 返回**最新** limit 条
   （升序，同毫秒按 rowid），limit>=1。
8. `expire_pending_approvals` 置 denied + 审计同事务；`list_pending_approvals` 不返回
   终态会话的遗留审批。
9. `latest_message_seq` 供检查点打点（回滚补偿集合按消息序号算，不是 created_at）。
10. 终态不可迁出：`failed/abandoned` 会话上拒绝 update/append/record/claim/approval。

## 2. B：tools / approval / audit / rollback

实现位置：`app/tools/`。

> **迁移提醒**：A 的 loop 只认 `common/api` 的**异步**端口；现有旧 `ToolRegistry`
> （同步、依赖 `runtime.py`）只供旧测试用。B 的分支已经提供了新实现
> （`DefaultToolRegistry` / `DefaultApprovalService` / `DefaultAuditService` /
> `create_async_builtin_registry`），**容器就是按这些名字探测的**，请保持：
>
> ```python
> from app.tools.registry import create_async_builtin_registry   # (workspace) -> ToolRegistry
> from app.tools.approval import DefaultApprovalService          # (state) -> ApprovalService
> from app.tools.audit import DefaultAuditService                # (state) -> AuditService
> ```
>
> 迁移完成后删除 `app/common/api/runtime.py` 与 `app/common/model/runtime.py`。

- `Tool`：`async execute(arguments) -> ToolResult`；写操作成功返回 `receipt`。
- `ToolRegistry`：`execute` 只执行不审批；`get` 未知工具抛 `ToolError`；
  `register` 同名抛 `ToolError`（运行期工具身份不变）。
- `ApprovalService`：策略 + 四个委托方法（全部委托 `StateStore`，不自开事务）。
- `AuditService`：**先对 Mapping 的值脱敏（`redact_values`）再 JSON 序列化**，再委托
  `record_audit`；审批决策的审计由 store 同事务写，不要重复记录。
- 回滚：补偿集合按消息序号（checkpoint.seq 之后 `role='tool'` 的消息对应调用 + reversible），
  `create_checkpoint` 的 seq 取 `latest_message_seq`，loop 在每次写操作派发前调用。

## 3. D：web / SSE / 前端

实现位置：`app/web/`、`app/main.py`（按 `sample.md` 结构）。

**容器期望的入口**：`app/web/sse/sse_event_publisher.py` 里的
`SseEventPublisher()`（无参构造，实现 `EventPublisher`）；D 的 `app/main.py` 用
`build_default_container()` 拿 `container.loop` / `container.subagents` / `container.events`。

**从旧原型迁移的对照表**（PR2 删除了 `app/core/agent.py` 等）：

| 旧（已删除） | 新 |
|---|---|
| `app.core.agent.Agent.run_agent(...)` | `container.loop.run_turn(session_id, user_content=...)`（`AgentLoop` 端口） |
| `app.core.provider.FakeProvider/OpenAIProvider/create_client` | `app/core/provider/` 包 + `provider_factory.create_provider(settings)`（测试注入 `client_factory`） |
| `app.common.model.runtime.ModelResponse/ToolCall/ToolResult` | `app.common.api.llm_provider` / `app.common.api.tool` 的 DTO |
| `app.common.event.runtime.MemoryEventSink` | `app.common.event.EventPublisher`（D 实现 `SseEventPublisher`） |
| `app.common.api.runtime.*` | `app.common.api` 端口 |
| 同步返回值 | `TurnOutcome`（`status/content/pending_call_id/pending_approval_id/iterations/tokens/error`） |

关键语义（与 PR2 一致）：

- `run_turn(session_id, user_content=...)`：会话有未完成轮 → 409（`ConflictError`）；
- `run_turn(session_id)`（恢复）：无 open turn → 409；审批未决策 → 返回 WAITING outcome；
  已决策 → 继续执行；
- `resume_turn(session_id, approval_id=None)`：审批决策后调用；不要自己派发工具；
- `abandon_turn`：放弃并过期待审批；
- 只读查询用 `peek_open_turn`（`load_open_turn` 会判 UNKNOWN 并写库）；
- `AgentEvent` 构造时校验 payload 白名单 + 自动脱敏，直接 `event.to_sse()`；
  事件 `seq` 对应 `messages.seq`（前端时间线）。
- 若最终采用 B 的 `MODEL_REQUEST` 事件，需要在 B 的分支合入后由 A 在
  `TurnEngine._model_step` 调 provider 前补发（payload `iteration` + `provider`）；
  当前 PR3 未加，避免在 `EventType` 尚未合入时引用不存在的枚举。

## 4. A：core（loop / provider / subagent / container）

已交付：

- **PR2**：`TurnEngine`（模型↔工具状态机、审批挂起/恢复、拒绝终止、UNKNOWN 恢复、
  事件 best-effort、prefix 稳定、迭代预算）、`DefaultAgentLoop`（会话锁/abandon）、
  `TurnProgress`；OpenAI/DeepSeek/Claude 适配器 + `provider_factory`；
- **PR3**：`DefaultSubagentScheduler`（深度/并发/预算/取消/崩溃恢复、运行记录、事件）、
  `SubagentContext`、`DelegationPolicy`（**子代理只读是硬边界**：`allowed_tools` 只能是
  `read_file`/`list_files`，写工具不能通过配置打开）、结果回流文本（按序列化后长度有界）；
  `container.py`（`Components`/`build_container`/`default_components`/`build_default_container`，
  探测实现时把「未交付」和「构造失败」都翻译成点名 `ConfigError`）、
  事件 `seq` 对齐 `messages.seq`（见 `AgentEvent` docstring 的 seq 语义）。

**子代理恢复与取消（D 接 Web 需要）**：`SubagentScheduler` 协议已含
`recover_unknown(session_id)`（启动/重连时对每个 open 会话调用一次，跳过本进程活跃
run）；`cancel(run_id)` 对终态 run 是空操作、对遗留 `running` 行标记 `cancelled`。
子代理状态以 `subagent_runs` 为唯一记录，REST 直接查这个表。

**事件 seq 约定**：`seq` 是消息锚点而非事件 id，同一消息的多个事件同 seq；
`seq=0` 表示不锚定持久化消息（子代理事件）；不完整模型响应不落库、也不发
`model_response`。

**已知限制（写进答辩要能解释）**：

- 子代理 transcript 不落库（schema 无 parent 列），落库的只有 `subagent_runs` 的
  brief/结果；崩溃遗留 `running` 由 `recover_unknown(session_id)` 判 `unknown`，不重试，
  且**跳过本进程仍在执行的 run**（D 在重连恢复时按会话调用）；
- 子代理不能执行写工具（无审批通道）；将来要开放写操作，必须注入真正的
  `ApprovalService` 并为子代理的每次调用新建审批记录，禁止复用父会话的批准；
- `token` 与 `reasoning_content` 不持久化（schema 无列）；
- `Checkpoint.state_json` 写入通道、`ToolResult` denied/skipped 拆分等留待后续清理 PR。
