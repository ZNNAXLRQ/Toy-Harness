# Toy Harness


## 环境搭建

前置只需要安装uv,然后拉取项目并安装依赖：

```powershell
git clone https://github.com/ZNNAXLRQ/Toy-Harness.git
cd Toy-Harness
uv sync          # 创建 .venv，按 uv.lock 安装运行依赖与开发依赖
```

`uv run` 会自动使用 `.venv`，不需要手动激活。

## 本机 Harness HTTP 入口

从仓库根目录运行（`--workspace` 必须指向已存在且可供读取的目录）：

```powershell
uv run python -m app.web --workspace "C:\path\to\readable-workspace" --offline
```

该入口监听 `127.0.0.1:8080`（可用 `--port` 修改）。省略 `--offline` 时，沿用 CLI Provider 的 `OPENAI_API_KEY` 和 `BASE_URL` 环境变量或仓库根目录 `.env`；`--offline` 使用每轮新建的 FakeProvider，执行一次真实的工作区列表读取并返回脚本化回复，不访问模型服务。不要把仓库根目录、存有凭据的目录或非信任方可修改的目录作为工作区；读取和脱敏不等于操作系统隔离。不要将入口代理暴露到公网。

- `/` 跳转至 `/html/index.html`；只挂载现有的 `/html`、`/css`、`/js`。静态页面仍是独立的演示模式，尚未调用下列 HTTP API。
- `GET /api/runtime` 返回 Provider 模式及单轮、只读、事件随结果返回的能力；`GET /api/tools` 从实际注册表返回工具 `schema` 和 `enabled`，只有 `read_file`、`list_files` 可执行。
- `POST /api/runs` 仅接受 `application/json` 和 `{"prompt":"…"}`（去掉首尾空白后 1–8192 字符）；不接受查询参数、工作区覆盖、密钥或模型端点配置。浏览器请求限同源；本机命令行客户端可不发送 `Origin`。
- 每轮返回 `result`（运行 ID、成败、最终答复、统计及错误代码）和本轮已产生的脱敏 `events` 摘要；不包含原始历史、工具输入输出或内部诊断。这不是实时流，也不支持恢复 CLI 或 Web 会话。出错时只提供稳定代码：`tool_denied`（403）、`run_failed`（502）随本轮结果返回；校验失败（422）、Provider 初始化失败（503）和入口内部失败（500）等在未能返回轮次结果时只提供顶层 `error`。

```powershell
Invoke-RestMethod http://127.0.0.1:8080/api/runs -Method Post -ContentType application/json -Body '{"prompt":"List my workspace"}'
```

## Web UI 本地预览（示例模式）

从仓库根目录运行：

```powershell
python -m http.server 8765 --bind 127.0.0.1 --directory static
```

在浏览器打开 `http://127.0.0.1:8765/html/index.html`，也可直接打开下表中的任意页面。`8765` 只提供静态文件；设置页的 `8080` 是 `4ad52fd` 中 `server.port` 的契约默认值，并非本演示监听的端口。无需启动 CLI、安装前端依赖、填写密钥或启动后端。每页的 HTML 和 CSS 同名配对，动态数据与交互由 `static/js/` 的外部模块处理。

九个页面各自只直接加载同名 CSS，由页样式导入 `static/css/sidebar.css`。外部模块从同一静态服务器加载 `static/html/sidebar.html`，挂载后才初始化会话导航；该文件只是片段，不是第十个页面。请通过上述 HTTP 地址打开页面，不要直接双击 HTML 文件。

侧栏按标题、新建会话、工作区、会话历史和页面跳转排列，底部「演示用户」仍通往设置。会话历史填满视口分配给侧栏的剩余高度；会话增减不会移动底部链接，超出的会话只在列表内滚动。视口太矮时，侧栏也可整体滚动，仍可用 Tab 到达各个入口。点击会话直接打开其聊天链接（例如 `/html/index.html?session=session-001`）；跨页链接保留当前会话，浏览器前进／后退也按链接恢复会话。新建会话会打开空白对话，不会代发消息。**工作区是演示适配层的分组，不映射到真实目录或 SQLite 数据库。**

演示使用 `sessionStorage` 保存当前标签页的所选会话、聊天历史、审批和检查点等状态；同源页面间导航或刷新会保留进度，不同会话的数据相互隔离。独立打开的新标签页从种子数据开始，因此只在原标签页新建的会话无法用链接在新标签页恢复；浏览器若在复制标签页或通过 opener 打开时复制初始状态，后续修改也不会互相同步。关闭标签页结束该次演示，或在开发者工具中清除本站的会话存储后刷新以重置。未发送的草稿和筛选器不跨页保存。演示不发起真实业务请求，也不会调用模型、执行工具或修改工作区文件。

| 页面路径（相对于静态服务器） | 数据对应（参考 `4ad52fd`） |
| --- | --- |
| `/html/index.html` | `StateStore` 消息、`LLMProvider.ModelResponse`、`AgentLoop.TurnOutcome`、`EventPublisher.AgentEvent` |
| `/html/approvals.html` | `ApprovalService.ApprovalRequest` 与决定、对应工具调用 |
| `/html/sessions.html` | `StateStore.SessionView`、历史和 `AgentLoop` 恢复入口 |
| `/html/checkpoints.html` | `RollbackService.Checkpoint`/`RollbackResult`、可补偿调用状态 |
| `/html/subagents.html` | `SubagentScheduler.SubagentTask`/`SubagentResult` |
| `/html/tools.html` | `ToolRegistry.schemas` 的函数 Schema，只读 |
| `/html/audit.html` | `AuditService.AuditEntry`，支持按会话范围与动作筛选 |
| `/html/context.html` | `ContextManager.PreparedContext`、`AgentsMdLoader.AgentsDocument`、`StateStore.TurnState` |
| `/html/settings.html` | `HarnessSettings` 只读示例，不展示、提交或保存密钥 |

快速体验：审批、检查点、子代理和审计默认显示「全部会话」，按创建时间倒序；切换到「当前会话」只看正在打开的会话。即使打开的是其他会话，也可在审批页审阅「便笺更新」的请求；批准或拒绝只记录决定，之后再点该行的「恢复演示轮次」。检查点页可以选择该会话的「更新便笺前」做模拟回滚，聊天历史仍保留。「中断恢复示例」可在聊天或上下文页直接继续，无需审批。上下文始终跟随当前会话，工具目录则是全局的。

点击侧栏「演示用户」进入只读设置，用左右方向键或 Home / End 切换「模型配置」和「运行环境」，Tab 进入当前面板。在弹窗中可用 Tab / Shift+Tab 循环焦点、Esc 关闭并返回触发按钮。窄窗口下侧栏导航和聊天/事件改为纵向布局。

定向测试（Node.js 20+，无额外依赖）：

```powershell
node --test tests/web-ui
```

浏览器验收另需 Node.js 22+ 和本机 Chromium 系浏览器。将 `TOY_HARNESS_BROWSER` 设为 Chrome、Edge 或 Chromium 的可执行文件绝对路径后运行同一命令；未设置时仅跳过浏览器用例，静态约束与服务回归仍会执行。

### 将来接入 REST / SSE

在 `static/js/service.js` 的装配点换掉演示适配器，保留异步查询、命令，以及 `subscribe(sessionId, listener) → unsubscribe` 的 UI 接口。`getSession` 是 UI 的聚合快照，不是宣称已存在的 HTTP 路径；应按最终 Web 契约取会话、消息、工具调用、审批等权威记录，同步后再接收该会话的 SSE 事件。断线、重复投递或切换会话时按权威快照校准、关闭旧订阅；不要把 SSE 内容当成持久状态，也不要把同一 `seq` 的不同事件视为重复。审批决定和恢复应继续分开，恢复时传入审批 ID。`demo.note` 是纯演示投影，不是服务端字段。当前演示页面不发出 REST/SSE 业务请求。
