# Toy Harness


## 环境搭建

前置只需要安装uv,然后拉取项目并安装依赖：

```powershell
git clone https://github.com/ZNNAXLRQ/Toy-Harness.git
cd Toy-Harness
uv sync          # 创建 .venv，按 uv.lock 安装运行依赖与开发依赖
```

`uv run` 会自动使用 `.venv`，不需要手动激活。

## Web UI 本地预览（示例模式）

从仓库根目录运行：

```powershell
python -m http.server 8765 --bind 127.0.0.1 --directory static
```

在浏览器打开 `http://127.0.0.1:8765/html/index.html`。`8765` 只提供静态文件；设置页的 `8080` 是 `4ad52fd` 中 `server.port` 的契约默认值，并非本演示监听的端口。无需启动 CLI、安装前端依赖、填写密钥或启动后端。页面执行的操作只改变当前标签页内存；刷新重新载入同一组种子数据。

| 页面片段 | 数据对应（参考 `4ad52fd`） |
| --- | --- |
| `#chat` | `StateStore` 消息、`LLMProvider.ModelResponse`、`AgentLoop.TurnOutcome`、`EventPublisher.AgentEvent` |
| `#approvals` | `ApprovalService.ApprovalRequest` 与决定、对应工具调用 |
| `#sessions` | `StateStore.SessionView`、历史和 `AgentLoop` 恢复入口 |
| `#checkpoints` | `RollbackService.Checkpoint`/`RollbackResult`、可补偿调用状态 |
| `#subagents` | `SubagentScheduler.SubagentTask`/`SubagentResult` |
| `#tools` | `ToolRegistry.schemas` 的函数 Schema，只读 |
| `#audit` | `AuditService.AuditEntry`，支持按会话范围与动作筛选 |
| `#context` | `ContextManager.PreparedContext`、`AgentsMdLoader.AgentsDocument`、`StateStore.TurnState` |
| `#settings` | `HarnessSettings` 只读示例，不展示、提交或保存密钥 |

快速体验：在「便笺更新」会话审阅请求，批准或拒绝后点「恢复演示轮次」；批准时可在检查点页验证内存便笺确实回滚而聊天历史保留。「中断恢复示例」可直接继续，无需审批。在弹窗中可用 Tab / Shift+Tab 循环焦点、Esc 关闭并返回触发按钮。窄窗口下侧栏导航和聊天/事件改为纵向布局。

定向测试（Node.js 20+，无额外依赖）：

```powershell
node --test tests/web-ui
```

### 将来接入 REST / SSE

只需在 `static/js/service.mjs` 的装配点换掉演示适配器，保留异步查询、命令，以及 `subscribe(sessionId, listener) → unsubscribe` 的 UI 接口。`getSession` 是 UI 的聚合快照，不是宣称已存在的 HTTP 路径；应按最终 Web 契约取会话、消息、工具调用、审批等权威记录，同步后再接收该会话的 SSE 事件。断线、重复投递或切换会话时按权威快照校准、关闭旧订阅；不要把 SSE 内容当成持久状态，也不要把同一 `seq` 的不同事件视为重复。审批决定和恢复应继续分开，恢复时传入审批 ID。`demo.note` 是纯演示投影，不是服务端字段。首版没有任何 REST/SSE 请求。
