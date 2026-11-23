# D 开工必读（3 分钟）

本目录属于 Web / SSE / 文档。开工前先读：

1. `app/common/api/IMPLEMENTATION_NOTES.md` 第 3 节（你的完整清单）。
2. `schema.sql` 新增了 `approvals` 表（审批要跨重启恢复）。审批的持久化由 C 的
   `StateStore` 负责，你只需要 HTTP 接口与页面。
3. `app/common/event/event_type.py`：13 个事件类型 + **payload 白名单/必填键**；
   `AgentEvent` 构造时会自动脱敏和校验，直接 `event.to_sse()` 推。

## 三个接口最关键

- **审批**：`GET /api/approvals?pending=1` → `POST /api/approvals/{id}`（decide）→
  紧接着调用 `AgentLoop.resume_turn(session_id)`（decide 本身不派发工具）。
- **重连快照**：会话状态 / messages / tool-calls / pending approvals 四类都要拉，
  只拉 messages 会漏掉审批与工具状态（它们变化时不产生消息）。
- **只读接口不要调 `load_open_turn`**（它会把正在执行的调用判成 UNKNOWN 并写库）；
  用 `peek_open_turn` 拿投影。

## 另外两件事

- `config/config.yaml` 已按 provider profile 写好，新增了 `workspace` / `agent` /
  `subagent` 三个 section；拼错键会直接报错（`extra="forbid"`）。
- README / `sample.md` 第三章的配置示例仍是旧格式，需要更新成 profile 形状。
