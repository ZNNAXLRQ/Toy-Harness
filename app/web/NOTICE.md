# D 开工必读（3 分钟）

本目录属于 Web / SSE / 文档。开工前先读：

1. `app/common/api/IMPLEMENTATION_NOTES.md` 第 3 节（完整清单 + **旧原型→新端口迁移对照表**）。
2. `schema.sql` 新增了 `approvals` 表（审批跨重启恢复）。持久化由 C 的 `StateStore` 负责，
   你只做 HTTP 接口与页面。
3. `app/common/event/event_type.py`：事件类型 + payload 白名单/必填键；
   `AgentEvent` 构造时校验 + 自动脱敏，直接 `event.to_sse()` 推；`seq` 对应 `messages.seq`。

> **`EventType.seq` 语义**：事件 `seq` = 它锚定的 `messages.seq`，**不是事件唯一 id**
> （同一条 assistant 消息的 `model_response`/`tool_start`/`tool_end` 同 seq）；`seq=0`
> 表示不锚定持久化消息（子代理 `subagent_started/finished`）。不完整模型响应不落库，
> 因此不发 `model_response`，只发 `turn_failed`——SSE 的 `id:` 请自己生成，别直接用 seq。
>
> **PR2 已删除旧执行链**（`app.core.agent` / `app.core.provider` / `app.main`）。
> `origin/web` 上基于旧 `Agent` 的 `app/web/runtime.py|api.py` **合并后会 import 失败**，
> 必须先迁移到 `AgentLoop` + `provider_factory` + `AgentEvent`（对照表见
> `IMPLEMENTATION_NOTES` 第 3 节）。

## 装配入口

- `app/core/config/container.py`：`build_default_container()` 探测 C/B/D 的实现并组装好
  `container.loop`（`AgentLoop`）、`container.subagents`（`SubagentScheduler`）、
  `container.events`（`EventPublisher`）。
- 你还需要提供 **`app/web/sse/sse_event_publisher.py` 里的 `SseEventPublisher()`**
  （无参构造，实现 `EventPublisher`）——容器按这个入口探测。
- 测试可以注入 `tests/fakes/` 里的替身（`ScriptedProvider`、`MemoryStateStore`、
  `RecordingEventPublisher`），不必等 C/B 的实现。

## 几个接口最关键

- **审批**：`GET /api/sessions/{id}/approvals?pending=1` →
  `POST /api/sessions/{id}/approvals/{approval_id}`（decide）→ 紧接着调用
  `AgentLoop.resume_turn(session_id)`（decide 本身不派发工具）。
- **重连快照**：会话状态 / messages / tool-calls / pending approvals 四类都要拉，
  只拉 messages 会漏掉审批与工具状态。
- **只读接口不要调 `load_open_turn`**（会把在飞调用判 UNKNOWN 并写库）；用 `peek_open_turn`。
- **会话还有未完成轮时，发新消息返回 409**（服务端 `ConflictError`）：先 `resume_turn`
  或 `abandon_turn`。`run_turn(session_id)`（不带消息）表示恢复：没有 open turn → 409；
  审批仍未决策 → 返回 WAITING 的 outcome（不是 409）；已决策 → 继续执行。
- `WAITING_APPROVAL` + `pending_approval_id` 非空 = **已决策、待恢复执行**（可能是
  approved 或 denied），页面显示「恢复中」，不要再列审批按钮；abandon 后的待审批已被
  过期，重连快照里不应出现。
- 会话已是 `failed/abandoned` 时，即使 tool-call 仍显示 `awaiting_approval`，也按
  「已终止」渲染（`expire_pending_approvals` 只改审批不改调用状态）。

## 另外三件事

- `config/config.yaml` 已按 provider profile 写好，新增 `workspace` / `agent` /
  `subagent` 三个 section；拼错键会直接报错（`extra="forbid"`，环境变量段名也有校验）。
- README / `sample.md` 第三章的配置示例仍是旧格式，需要更新成 profile 形状。
- 若最终采用 `MODEL_REQUEST` 事件（B 的 SSE 原型加的），要等 B 的分支合入后由 A 在
  `TurnEngine._model_step` 补发；当前 PR3 还没加，前端先按现有事件渲染。
