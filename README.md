# Toy Harness

当前已将 Nano Codex 的基础执行链拆分移植到项目；这是同步、内存状态的原型，不是完整 Web Harness。
各实现模块头部包含来源文件/代码段、已实现功能和后续完善项。

## 已移植

- 模型—工具 Agent 循环、完整消息回传和最终回复保存。
- OpenAI-compatible Provider、可编程 FakeProvider。
- UTF-8 文件读写、递归目录、命令和 Python 执行。
- 工具 Schema、注册校验、参数校验、统一结果和宿主审批接口。
- 显式工作区路径、子进程 `cwd`、基础敏感环境变量过滤和事件脱敏。
- 可注入内存事件接收器、统计、无网络 Demo 和标准库离线测试。

## 使用根目录虚拟环境（Windows PowerShell）

```powershell
Set-Location 'D:/Programs/Python/OOAD/Project'
& 'D:/Programs/Python/OOAD/.venv/Scripts/python.exe' -m pip --require-virtualenv install -r 'D:/Programs/Python/OOAD/Project/requirements.txt'
& 'D:/Programs/Python/OOAD/.venv/Scripts/python.exe' -B -m app.main --demo
& 'D:/Programs/Python/OOAD/.venv/Scripts/python.exe' -B -m unittest discover -s 'D:/Programs/Python/OOAD/Project/tests' -v
```

Demo 和测试均不调用真实模型 API。它们使用临时目录，不修改项目工作区。

## 可选真实模型运行

将 `D:/Programs/Python/OOAD/Project/.env.example` 复制为
`D:/Programs/Python/OOAD/Project/.env`，本地填写 `BASE_URL`、`OPENAI_API_KEY`。
不会自动读取或复制作业的密钥。Provider 沿用当前进程代理配置。

```powershell
Set-Location 'D:/Programs/Python/OOAD/Project'
& 'D:/Programs/Python/OOAD/.venv/Scripts/python.exe' -B -m app.main --workspace 'D:/Programs/Python/OOAD/Project/workspace' '列出当前工作区'
```

默认拒绝写文件、执行命令和运行 Python；读取/列目录不需要审批。
添加 `--interactive-approval` 后由终端用户逐条确认敏感调用。拒绝会停止当前 turn，
不会继续执行同一批的剩余工具。该终端适配器将来应由持久化 Web 审批替换。

## 尚未实现

SQLite/会话持久化、REST/SSE、上下文预算与要求记忆、检查点、Resume、回滚及子代理。
`D:/Programs/Python/OOAD/Project/schema.sql`、`D:/Programs/Python/OOAD/Project/config/config.yaml`
和 `D:/Programs/Python/OOAD/Project/pyproject.toml` 暂未填充，避免将原型误当成最终设计。

**安全边界：**文件工具拒绝工作区越界和链接，但任意命令/Python 程序并不是沙箱，仍可访问
宿主权限允许的外部资源；仅对可信的本地操作授权。环境变量过滤和日志脱敏不是密钥隔离机制，
原始消息仍含原始内容，不能不经处理直接向外暴露。

模块来源和后续任务见 `D:/Programs/Python/OOAD/Project/docs/nano-codex-migration.md`。
