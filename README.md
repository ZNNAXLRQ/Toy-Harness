# Toy Harness


## 环境搭建

前置只需要安装uv,然后拉取项目并安装依赖：

```powershell
git clone https://github.com/ZNNAXLRQ/Toy-Harness.git
cd Toy-Harness
uv sync          # 创建 .venv，按 uv.lock 安装运行依赖与开发依赖
```

`uv run` 会自动使用 `.venv`，不需要手动激活。

## Web SSE 接口

在仓库根目录启动接口服务。

```powershell
uv run uvicorn app.web.server:app --host 127.0.0.1 --port 8080
```

- `GET /api/config` 返回当前模型提供方和离线测试任务。
- `POST /api/runs` 接收 `prompt` 和 `mode`，返回 `run_id`。权限模式为 `approval` 或 `full`。
- `GET /api/runs/{run_id}/events` 返回 SSE 事件流。
- `POST /api/runs/{run_id}/approvals/{approval_id}` 接收 `decision`，值为 `approved` 或 `denied`。

每次运行使用独立的临时工作区，初始文件是 `notes.txt`。会话与审批历史随服务退出清除。

如需让 DeepSeek Flash 选择工具，在进程环境中或仓库本地的 `.env` 中配置 `DEEPSEEK_API_KEY`，然后以真实模型模式启动。

```powershell
$env:TOY_HARNESS_PROVIDER = "deepseek-flash"
uv run uvicorn app.web.server:app --host 127.0.0.1 --port 8080
```

运行以下测试可离线验证两种权限模式和事件流。

```powershell
uv run pytest -q tests/test_web_sse.py tests/test_web_providers.py tests/test_event_contract.py
```
