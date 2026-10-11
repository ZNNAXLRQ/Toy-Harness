下面是一份 **Toy Harness 项目计划（Python 版）**。  
除了把 Java/Maven/Spring Boot 换成 Python/FastAPI/SQLite 之外，**功能、高级功能、四人分工、协作规则、时间轴、测试策略都不变**。

---

## 一、总体结构

```text
toy-harness/
├── pyproject.toml          # A 维护，唯一依赖与构建配置
├── requirements.txt        # A 维护，可选，便于 pip install
├── README.md               # D
├── docs/                   # D
├── scripts/                # D
├── data/                   # 运行时生成，不提交
├── .gitignore              # D
├── schema.sql              # C
├── config/
│   └── config.yaml         # D
├── static/                 # D
│   └── index.html
└── app/
    ├── __init__.py
    ├── main.py             # D，FastAPI 启动
    ├── common/             # A + C
    │   ├── __init__.py
    │   ├── api/            # A
    │   ├── model/          # C
    │   ├── event/          # A
    │   └── exception/      # A
    ├── core/               # A
    ├── tools/              # B
    ├── state/              # C
    └── web/                # D
```

依赖方向：

```text
common ← core
common ← tools
common ← state
core + tools + state + common ← web
```

`web` 是组装层，其他模块之间只依赖 `common`，避免循环依赖。

---

## 二、依赖与构建（A 维护）

Python 不再用 Maven，改用 `pyproject.toml` + `uv` 或 `pip`。  
推荐 Python 3.11+，FastAPI + Uvicorn。

`pyproject.toml`：

```toml
[project]
name = "toy-harness"
version = "1.0.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "httpx>=0.27",
    "sse-starlette>=2.1",
    "pyyaml>=6.0",
]

[tool.uv]
dev-dependencies = []
```

`requirements.txt` 可选：

```text
fastapi>=0.115
uvicorn[standard]>=0.32
pydantic>=2.9
httpx>=0.27
sse-starlette>=2.1
pyyaml>=6.0
```

只有 A 能改 `pyproject.toml` / `requirements.txt`，其他人需要新依赖时告诉 A。

---

## 三、配置（D 维护）

`config/config.yaml`：

```yaml
server:
  port: 8080

database:
  path: ./data/harness.db

llm:
  provider: deepseek
  api_key: ${DEEPSEEK_API_KEY}
  base_url: https://api.deepseek.com

logging:
  level: INFO
```

启动时读取配置，并自动创建 `data/harness.db`、执行 `schema.sql`。

---

## 四、schema.sql（C 维护）

位置：项目根目录 `schema.sql`。内容与 Java 版完全一致：

```sql
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    status TEXT,
    agents_md_version TEXT,
    prefix_hash TEXT,
    created_at INTEGER,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    message_id TEXT,
    tool_name TEXT NOT NULL,
    arguments TEXT,
    status TEXT,
    reversible INTEGER,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    tool_call_id TEXT NOT NULL,
    content TEXT,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    seq INTEGER,
    state_json TEXT,
    description TEXT,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    actor TEXT,
    action TEXT,
    detail TEXT,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS subagent_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    parent_task_id TEXT,
    status TEXT,
    context_json TEXT,
    result_json TEXT,
    created_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_checkpoints_session ON checkpoints(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_logs(session_id);
```

---

## 五、完整目录结构与模块说明

### 1. 根目录

| 路径 | 作用 | 负责人 |
|---|---|---|
| `pyproject.toml` | 唯一依赖与构建配置 | A |
| `requirements.txt` | 可选，便于 pip | A |
| `README.md` | 安装、运行、示例 | D |
| `docs/` | 架构、API、演示、报告 | D |
| `scripts/` | 实操测试脚本 | D |
| `data/` | SQLite 文件，运行时生成 | 不提交 |
| `.gitignore` | 忽略 `data/`、`__pycache__/`、`.venv/` | D |
| `schema.sql` | 建表 | C |
| `config/config.yaml` | 配置 | D |
| `static/index.html` | 简单前端 | D |

---

### 2. 启动文件

| 路径 | 作用 | 负责人 |
|---|---|---|
| `app/main.py` | FastAPI 启动入口 | D |

```python
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.state.persistence.db import init_db
from app.web.controller import session, chat, approval, rollback, tool, subagent, audit
from app.web.sse.event_stream import router as sse_router

app = FastAPI(title="Toy Harness")

@app.on_event("startup")
def startup():
    init_db()

app.include_router(session.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(approval.router, prefix="/api")
app.include_router(rollback.router, prefix="/api")
app.include_router(tool.router, prefix="/api")
app.include_router(subagent.router, prefix="/api")
app.include_router(audit.router, prefix="/api")
app.include_router(sse_router, prefix="/api")

app.mount("/static", StaticFiles(directory="static"), name="static")
```

启动命令：

```bash
uvicorn app.main:app --reload --port 8080
```

---

### 3. common 包（A + C）

| 子包 | 作用 | 负责人 |
|---|---|---|
| `app/common/api` | 抽象接口 | A |
| `app/common/model` | 领域模型 | C |
| `app/common/event` | 事件定义 | A |
| `app/common/exception` | 通用异常 | A |

#### common/api（A）

| 文件 | 作用 |
|---|---|
| `llm_provider.py` | 模型调用抽象基类 |
| `agent_loop.py` | Agent 主循环接口 |
| `tool.py` | 工具接口，含 `rollback` |
| `tool_registry.py` | 工具注册表 |
| `context_manager.py` | 上下文管理 |
| `state_store.py` | 状态持久化 |
| `approval_service.py` | 审批服务 |
| `rollback_service.py` | 回滚服务 |
| `audit_service.py` | 审计服务 |
| `subagent_scheduler.py` | 子代理调度 |
| `agents_md_loader.py` | AGENTS.md 加载 |

#### common/model（C）

| 文件 | 作用 |
|---|---|
| `message.py` | 单条消息 |
| `role.py` | 角色枚举 |
| `tool_call.py` | 工具调用记录 |
| `observation.py` | 工具观察结果 |
| `task_state.py` | 任务状态 |
| `session.py` | 会话 |
| `checkpoint.py` | 检查点 |
| `subagent_result.py` | 子代理结果 |

建议用 Pydantic 模型或 `@dataclass`。

#### common/event（A）

| 文件 | 作用 |
|---|---|
| `event_type.py` | 事件类型枚举 |
| `agent_event.py` | 事件对象 |
| `event_publisher.py` | 事件发布接口，SSE 用 |

#### common/exception（A）

| 文件 | 作用 |
|---|---|
| `harness_exception.py` | 通用异常 |

---

### 4. core 包（A）

| 子包 | 作用 | 负责人 |
|---|---|---|
| `app/core/loop` | Agent 主循环 | A |
| `app/core/provider` | 模型适配 | A |
| `app/core/subagent` | 子代理调度 | A |
| `app/core/config` | 依赖组装 | A |

#### core/loop

| 文件 | 作用 |
|---|---|
| `agent_loop_engine.py` | 主循环引擎 |
| `default_agent_loop.py` | 默认实现 |
| `turn_state.py` | 单轮状态机 |

#### core/provider

| 文件 | 作用 |
|---|---|
| `openai_provider.py` | OpenAI 适配 |
| `deepseek_provider.py` | DeepSeek 适配 |
| `claude_provider.py` | Claude 适配 |
| `provider_factory.py` | 按配置选 provider |

#### core/subagent

| 文件 | 作用 |
|---|---|
| `default_subagent_scheduler.py` | 子代理调度实现 |
| `subagent_context.py` | 子代理独立上下文 |
| `delegation_policy.py` | 委派策略 |
| `subagent_result_merger.py` | 结果汇总 |

#### core/config

| 文件 | 作用 |
|---|---|
| `container.py` | 手动依赖注入 / 工厂 |

---

### 5. tools 包（B）

| 子包 | 作用 | 负责人 |
|---|---|---|
| `app/tools/registry` | 工具注册与插件 | B |
| `app/tools/builtin` | 内置工具 | B |
| `app/tools/approval` | 审批 | B |
| `app/tools/audit` | 审计日志 | B |
| `app/tools/rollback` | 检查点与回滚 | B |

#### tools/registry

| 文件 | 作用 |
|---|---|
| `default_tool_registry.py` | 工具注册实现 |
| `tool_plugin.py` | 插件式工具扩展 |

#### tools/builtin

| 文件 | 作用 |
|---|---|
| `bash_tool.py` | 执行 bash |
| `file_read_tool.py` | 读文件 |
| `file_write_tool.py` | 写文件 |
| `search_tool.py` | 搜索 |
| `http_tool.py` | HTTP 请求 |

#### tools/approval

| 文件 | 作用 |
|---|---|
| `default_approval_service.py` | 审批实现 |
| `permission_policy.py` | 权限策略 |

#### tools/audit

| 文件 | 作用 |
|---|---|
| `default_audit_service.py` | 审计实现，写 `audit_logs` |
| `audit_event.py` | 审计事件 |

#### tools/rollback

| 文件 | 作用 |
|---|---|
| `default_rollback_service.py` | 回滚实现 |
| `checkpoint_service.py` | 检查点创建与恢复 |
| `compensating_action.py` | 写操作补偿 |

---

### 6. state 包（C）

| 子包 | 作用 | 负责人 |
|---|---|---|
| `app/state/context` | 上下文管理 | C |
| `app/state/session` | 会话与状态 | C |
| `app/state/persistence` | SQLite 持久化 | C |
| `app/state/resume` | 断点恢复 | C |
| `app/state/agents` | AGENTS.md / Skills | C |
| `app/state/cache` | Prefix-cache | C |

#### state/context

| 文件 | 作用 |
|---|---|
| `default_context_manager.py` | 上下文管理实现 |
| `token_budget.py` | token 预算 |
| `summarizer.py` | 历史摘要 |

#### state/session

| 文件 | 作用 |
|---|---|
| `default_state_store.py` | 状态存储实现 |
| `session_service.py` | 会话服务 |
| `task_state_service.py` | 任务状态服务 |

#### state/persistence

| 文件 | 作用 |
|---|---|
| `db.py` | SQLite 连接与 `init_db` |
| `session_repository.py` | sessions 表 |
| `message_repository.py` | messages 表 |
| `tool_call_repository.py` | tool_calls 表 |
| `observation_repository.py` | observations 表 |
| `checkpoint_repository.py` | checkpoints 表 |
| `audit_log_repository.py` | audit_logs 表 |
| `subagent_run_repository.py` | subagent_runs 表 |

全部使用 Python 标准库 `sqlite3`。

#### state/resume

| 文件 | 作用 |
|---|---|
| `resume_service.py` | 从 SQLite 恢复会话与任务 |

#### state/agents

| 文件 | 作用 |
|---|---|
| `default_agents_md_loader.py` | 加载 AGENTS.md |
| `skill_registry.py` | 技能注册 |

#### state/cache

| 文件 | 作用 |
|---|---|
| `stable_prefix_builder.py` | 构建稳定前缀 |
| `prefix_cache_policy.py` | 缓存策略 |

---

### 7. web 包（D）

| 子包 | 作用 | 负责人 |
|---|---|---|
| `app/web/controller` | REST API | D |
| `app/web/sse` | SSE 事件流 | D |
| `app/web/dto` | 请求/响应 DTO | D |
| `app/web/config` | Web 配置 | D |

#### web/controller

| 文件 | 作用 |
|---|---|
| `session.py` | 会话 CRUD |
| `chat.py` | 发送消息 |
| `approval.py` | 审批 |
| `rollback.py` | 回滚 |
| `tool.py` | 工具列表 |
| `subagent.py` | 子代理状态 |
| `audit.py` | 审计查询 |

使用 FastAPI `APIRouter`。

#### web/sse

| 文件 | 作用 |
|---|---|
| `event_stream.py` | `/api/sessions/{id}/events` |
| `sse_event_publisher.py` | 实现 `EventPublisher` |

可用 `sse-starlette` 或 `StreamingResponse`。

#### web/dto

| 文件 | 作用 |
|---|---|
| `create_session_request.py` | 创建会话请求 |
| `send_message_request.py` | 发送消息请求 |
| `approval_request.py` | 审批请求 |
| `api_response.py` | 统一响应 |

使用 Pydantic 模型。

#### web/config

| 文件 | 作用 |
|---|---|
| `web_config.py` | CORS、静态资源 |
| `openapi_config.py` | Swagger 配置 |

---

## 六、SQLite 配置参考代码

### 1. `app/state/persistence/db.py`

```python
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path("./data/harness.db")
SCHEMA_PATH = Path("schema.sql")

@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = f.read()
    with get_conn() as conn:
        conn.executescript(schema)
```

### 2. `session_repository.py`（C 参考）

```python
from app.state.persistence.db import get_conn
from app.common.model.session import Session

class SessionRepository:
    def save(self, s: Session):
        with get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sessions
                (id, title, status, agents_md_version, prefix_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (s.id, s.title, s.status, s.agents_md_version, s.prefix_hash,
                 s.created_at, s.updated_at)
            )

    def find_by_id(self, session_id: str):
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return Session(**dict(row)) if row else None

    def find_all(self):
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
            return [Session(**dict(row)) for row in rows]
```

### 3. `message_repository.py` 示例

```python
class MessageRepository:
    def save(self, m):
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO messages
                (id, session_id, seq, role, content, tool_call_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (m.id, m.session_id, m.seq, m.role.value,
                 m.content, m.tool_call_id, m.created_at)
            )

    def find_by_session(self, session_id: str):
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY seq ASC",
                (session_id,)
            ).fetchall()
            return [Message(**dict(row)) for row in rows]
```

### 4. 审计写入示例（B 参考）

```python
class DefaultAuditService:
    def __init__(self, repository):
        self.repository = repository

    def log(self, session_id, actor, action, detail):
        import uuid, time
        self.repository.save({
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "actor": actor,
            "action": action,
            "detail": detail,
            "created_at": int(time.time() * 1000),
        })
```

---

## 七、四人分工总结

### A：架构 / Core / Subagents
负责：
- `pyproject.toml`、`requirements.txt`
- `app/common/api`
- `app/common/event`
- `app/common/exception`
- `app/core/`

任务：
- Agent Loop、Provider 适配
- Subagents 调度、委派、结果汇总
- AGENTS.md 集成、Prefix-cache 架构配合
- 定义所有跨模块接口

### B：Tools / Audit / Rollback
负责：
- `app/tools/`

任务：
- 工具注册、bash/文件/搜索/HTTP
- 审批、权限
- 审计日志写 `audit_logs`
- 插件式工具注册
- 检查点、Rollback、补偿

### C：State / Persistence / Resume / AGENTS.md
负责：
- `app/common/model`
- `app/state/`
- `schema.sql`

任务：
- 上下文管理、token 预算、摘要
- 会话、任务状态、SQLite 持久化
- Resume 断点恢复
- AGENTS.md / Skills
- Prefix-cache 设计

### D：Web / REST / SSE / 文档 / 演示
负责：
- `app/main.py`
- `app/web/`
- `config/config.yaml`
- `static/`
- `docs/`
- `scripts/`
- `README.md`

任务：
- REST API、SSE 事件流
- Web UI 中间进度、审批、回滚、子代理可视化
- 综合测试
- 必要的说明文档主负责人，和期末答辩演示主负责人

### 协作规则
1. 每人只改自己负责的文件夹，创建各自的分支用于开发，完成自测无问题后再 merge 到 dev 分支。
2. `pyproject.toml`、`config/config.yaml`、`schema.sql` 由负责人主要维护，其他人不要在未通知的情况下贸然修改。
3. 跨模块调用通过抽象接口 + 依赖注入，不要直接 `new` 实现类。
4. 多提交，别攒了几天的修改一次提交，既方便其他人跟进进度也便于出问题回滚，有需要直接群 @，尽可能优先处理他人需求的类，减少无谓的等待。