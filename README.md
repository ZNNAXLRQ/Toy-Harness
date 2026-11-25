# Toy Harness


## 环境搭建

前置只需要安装uv,然后拉取项目并安装依赖：

```powershell
git clone https://github.com/ZNNAXLRQ/Toy-Harness.git
cd Toy-Harness
uv sync          # 创建 .venv，按 uv.lock 安装运行依赖与开发依赖
```

`uv run` 会自动使用 `.venv`，不需要手动激活。

## Web 实时演示

在仓库根目录启动本地服务，然后打开 [Toy Harness](http://127.0.0.1:8080/)。

```powershell
uv run uvicorn app.web.server:app --host 127.0.0.1 --port 8080
```

默认使用固定的离线示例。点击运行后，右侧时间线会随 SSE 事件显示模型请求、工具选择、审批、工具执行和本轮结果。展开工具条目可查看参数及结果。

- **需要审批**会自动读取文件，在执行 `write_file` 前等待明确的批准。拒绝后结束本轮。
- **全权限**会直接执行网页开放的三种工具 `read_file`、`list_files` 和 `write_file`。

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
