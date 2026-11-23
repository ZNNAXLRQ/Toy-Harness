# D 开工必读（3 分钟）

本目录属于 Web / SSE / 文档。开工前先读：

1. `app/common/api/IMPLEMENTATION_NOTES.md` 第 3 节（完整清单）。
2. `schema.sql` 新增了 `approvals` 表（审批跨重启恢复）。持久化由 C 的 `StateStore` 负责，
   你只做 HTTP 接口与页面。
3. `app/common/event/event_type.py`：13 个事件类型 + payload 白名单/必填键；
   `AgentEvent` 构造时校验 + 脱敏，直接 `event.to_sse()` 推。

## 三个接口最关键

- **审批**：`GET /api/sessions/{id}/approvals?pending=1` →
  `POST /api/sessions/{id}/approvals/{approval_id}`（decide）→ 紧接着调用
  `AgentLoop.resume_turn(session_id)`（decide 本身不派发工具）。
- **重连快照**：会话状态 / messages / tool-calls / pending approvals 四类都要拉，
  只拉 messages 会漏掉审批与工具状态。
- **只读接口不要调 `load_open_turn`**（会把在飞调用判 UNKNOWN 并写库）；用 `peek_open_turn`。

## 另外两件事

- `config/config.yaml` 已按 provider profile 写好，新增 `workspace` / `agent` /
  `subagent` 三个 section；拼错键会直接报错（`extra="forbid"`，环境变量段名也有校验）。
- README / `sample.md` 第三章的配置示例仍是旧格式，需要更新成 profile 形状。
