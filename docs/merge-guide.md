# 合并与整合说明（B / C / D 必读）

> 状态基准：`main = 626c579`（PR #2：PR2 已合并）；`feat/a-subagent`（PR3）基于它。
> 本文件随 PR3 一起合入。**合并顺序有依赖，不要打乱。**

## 1. 现在 main 上有什么、还缺什么

| 模块 | 状态 |
|---|---|
| `app/common/api/*` 端口 + 契约测试 | ✅ 已合并（PR #1） |
| `app/core/loop`（Agent 主循环）、`app/core/provider`（OpenAI/DeepSeek/Claude 适配）、删除原型执行链 | ✅ 已合并（PR #2） |
| `app/core/subagent`（子代理调度）、`app/core/config/container.py`（装配）、事件 seq 对齐、D 的迁移对照表 | ⏳ 本 PR（PR3） |
| `app/state/*` 的 SQLite 实现（C） | ❌ 未交付（契约 skip 中） |
| `app/tools/*` 的异步端口实现（B） | 🔶 在 `feat/web-sse` 上，未合并 |
| `app/web/*` + `app/main.py`（D） | 🔶 在 `web` / `feat/web-sse` 上，基于旧原型，合并后必坏，必须先迁移 |

## 2. 合并顺序（唯一推荐顺序）

```text
PR3（feat/a-subagent）  →  B 的 tools 分支（feat/web-sse，剥完前端后）  →  D 的 web 分支
```

理由：

1. **PR3 是地基**：`container.py` 定义了 B/C/D 的接入契约（探测入口写死在里面），
   越早合并，后面的分支只需适配、不用猜；
2. **B 的 tools 层已经按端口实现**（`create_async_builtin_registry` /
   `DefaultApprovalService` / `DefaultAuditService`），容器就是按这些名字探测的；
   合并后可立刻让 main 的 `default_components()` 少报两个"未交付"；
3. **D 最后**：D 的 `app/web` 目前 import 了 PR2 已删除的 `app.core.agent` /
   `app.core.provider` / `app.main`，原样合并会让 19 个 web 测试直接 collection error；
   必须在 PR3 合并后按 `app/common/api/IMPLEMENTATION_NOTES.md` 第 3 节的对照表迁移
   （或用 `container` 装配），再合并。

> B 与 D 之间关于"谁做最终 Web 入口/前端归属"的分工是团队决定，不属于本说明范围；
> 但**两套 Web 实现不能同时作为最终入口**（都占 8080、都提供 `/`、`POST /api/runs`
> 语义互斥）。合并 README 时请按最终选择保留一节。

## 3. PR3 合并前（A 自查，已做）

- [x] 全量测试：`uv run pytest -q` → `220 passed, 67 skipped, 19 subtests passed`
- [x] 契约：`uv run pytest tests/contracts -q` → 66 条 × 2 实现（sqlite 侧 skip 等 C）
- [x] `uv lock --check`、无 BOM、工作区干净
- [x] rebase 到最新 main（`626c579`），线性 3 个提交，无冲突
- [x] auditor 验收：无 P0/P1

## 4. 各人 rebase 后必须做的事

### C（state / persistence，优先级最高）

实现并交付：

- `StateStore`：过 `tests/contracts/test_state_store_contract.py` 的 61 条；
- `SubagentRunStore`：过 `tests/contracts/test_subagent_run_contract.py` 的 5 条；
- `ContextManager`：`prepare/build_summary/record_usage/should_compact`。

容器期望入口（`app/core/config/container.py` 里写死，缺了会点名报错到人）：

```python
# app/state/persistence/factory.py
def create_stores(settings) -> tuple[StateStore, SubagentRunStore]: ...

# app/state/context/factory.py
def create_context_manager(settings) -> ContextManager: ...

# app/state/agents/default_agents_md_loader.py（可选）
class DefaultAgentsMdLoader: ...  # 无参构造
```

接入点：把 `tests/contracts/conftest.py` 的 `_sqlite_store()` /
`_sqlite_subagent_store()` 两个 skip 换成真实构造。**不要改契约测试本身**（除接入点）。
细节见 `app/state/NOTICE.md`。

### B（tools / approval / audit / rollback）

1. 剥完前端后 rebase 到最新 main；
2. 容器探测名**保持不变**（PR3 已按这些名字装配）：

   ```python
   from app.tools.registry import create_async_builtin_registry   # (workspace) -> ToolRegistry
   from app.tools.approval import DefaultApprovalService          # (state) -> ApprovalService
   from app.tools.audit import DefaultAuditService                # (state) -> AuditService
   ```

3. 迁移/删除旧链路：
   - `app/web/providers.py` 里 `from app.core.provider import DEFAULT_MODEL, OpenAIProvider`
     在 PR2 之后是 **ImportError**（`app/core/provider.py` 已变成同名的包，名字被占用），
     改成 `app.core.provider.openai_provider.OpenAIChatProvider` 或
     `provider_factory.create_provider(settings)`；
   - 旧同步 `ToolRegistry` / `create_builtin_registry`、`DenyApproval`/`TerminalApproval`
     迁移完成后删除；
   - 确认没有新代码 import `app.common.api.runtime` / `app.common.model.runtime`
     （这两个旧注入点等 B 迁移完由 A 删除）；
   - 如果保留 `app/web/server.py` 的 live 模式：它调用的是**同步** `provider.complete(messages, tools)`，
     与 PR2 的 `async complete(CompletionRequest)` 不兼容，要么改成异步适配层，
     要么把 server 明确降级为离线 demo（README 里写清）。

4. 验收：`uv run pytest -q`（合并 B 后预期全绿）；merge 后 `tests/test_container.py`
   已经预适配（不再假设 B 未交付），若红了说明探测名/签名变了。

### D（web / SSE / 前端）

1. rebase 到含 PR2+PR3 的 main，然后**先迁移后端再合并**：
   - `app/web/runtime.py` / `api.py` 里的 `app.core.agent.Agent` /
     `app.core.provider.*` / `app.common.api.runtime` / `MemoryEventSink` 全部替换：
     - 跑一轮 → `container.loop.run_turn(...)`（`AgentLoop` 端口）；
     - 恢复/审批 → `container.loop.resume_turn(...)` / `abandon_turn(...)`；
     - 事件 → `container.events`（`EventPublisher`）；
     - 模型 → 不要自己建 client，用容器/`provider_factory`；
   - 完整对照表在 `app/common/api/IMPLEMENTATION_NOTES.md` 第 3 节；
2. 提供 **`app/web/sse/sse_event_publisher.py` 的 `SseEventPublisher()`**（无参构造，
   实现 `EventPublisher`）——`build_default_container()` 按这个入口探测；
3. 事件 seq 语义（写进 `AgentEvent` docstring）：`seq` 是**消息锚点**，不是事件 id；
   同一消息可对应多个事件同 seq；`seq=0` = 不锚定持久化消息（子代理事件）。
   SSE 的 `id:` 请自己生成，不要直接用 seq；
4. 子代理 REST：状态唯一来源是 `subagent_runs` 表；启动/重连时对 open 会话调用
   `SubagentScheduler.recover_unknown(session_id)`；取消用 `cancel(run_id)`；
5. 已删除的模块**不要恢复**：`app/core/agent.py`、`app/core/provider.py`、
   `app/main.py`、`app/common/event/runtime.py`；不要加兼容 shim。

## 5. 已知跨分支问题（合并时会遇到，先有心理预期）

| 问题 | 现象 | 处理 |
|---|---|---|
| D 的旧后端 | 合并后 `tests/test_web_api.py` collection error（`app.core.agent` 不存在；`app.core.provider` 是包，`from ... import FakeProvider` 报 ImportError） | D 按 §4 迁移，不要"恢复旧文件"（包会遮蔽同名模块，恢复也没用） |
| B 的 `providers.py` | 合并后 `test_web_providers` 3 条 + `test_web_sse` 8 条失败 | 3 行 import/模型迁移，见 §4 B-3 |
| B 的 live 模式 | demo 测试全绿但 live 调 provider 会坏（同步 vs 异步） | 改异步适配或降级为离线 demo |
| B × D 的 README | 两个分支在同一位置追加运行说明，唯一文本冲突 | 按最终 Web 入口保留一节，另一节移到 docs 或删除 |
| `MODEL_REQUEST` 事件 | B 的 SSE 时间线期望它；A 的 `TurnEngine` 目前不发（PR3 未加，因为枚举在 B 分支上） | B 分支合并后，由 A 在 `_model_step` 调 provider 前补发（payload `iteration` + `provider`）；合入前 D 前端先按现有事件渲染 |
| `Checkpoint` / 回滚 | `CheckpointStore` 由 C 交付；B 的 `DefaultRollbackService` 已按端口写但未跑通 | C 交付后接 `container`，再跑回滚验收 |

## 6. 合并后的验收命令（谁合并谁跑）

```powershell
uv sync --locked
uv run --no-sync pytest -q                 # 期望：全绿（C 交付前 sqlite 契约仍 skip）
uv run --no-sync pytest tests/contracts -q # 66 条 × 2 实现
uv lock --check
```

CI（`.github/workflows/ci.yml`）只在 main/dev 上触发；**合并到 main 前请在本地跑上面三条**。

## 7. 不要做的事

- ❌ 不要恢复 PR2 删除的原型模块（`app/core/agent.py`、`app/core/provider.py`、
  `app/main.py`、`app/common/event/runtime.py`）；
- ❌ 不要为兼容旧 Web 加 shim/包装层——D 迁移到端口才是正解；
- ❌ 不要新写代码 import `app/common/api/runtime.py` 或 `app/common/model/runtime.py`；
- ❌ 不要改 `tests/contracts/` 的契约用例来"适配"实现（只改 conftest 的两个接入点）；
- ❌ 不要同时合并两套 Web 入口。

## 8. 有疑问看哪里

- 端口契约与实现指南：`app/common/api/IMPLEMENTATION_NOTES.md`
- C 开工：`app/state/NOTICE.md`
- B 开工：`app/tools/NOTICE.md`
- D 开工：`app/web/NOTICE.md`
- 原型迁移记录：`docs/nano-codex-migration.md`
- 装配入口：`app/core/config/container.py`（缺什么会点名报错到人）
