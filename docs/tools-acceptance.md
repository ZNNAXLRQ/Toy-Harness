# B 模块验收

在仓库根目录安装锁定的依赖并运行离线验收。

```sh
uv sync --locked
uv run --no-sync pytest -v tests/test_b_tools.py tests/test_audit_service.py tests/test_tools.py
```

| 场景 | 通过标准 |
| --- | --- |
| 工具执行 | 读取中文文件。参数无效或路径越过工作区时，文件保持原样。同名工具不能覆盖。 |
| 审批 | 待审和拒绝都不修改文件。批准后只派发一次，重复提交决定会报错。 |
| 审计 | 按会话查到审批和工具结果，审批决定只出现一次。密钥在 JSON 序列化之前脱敏。 |
| 文件恢复 | 两次写入按消息顺序逆序补偿，恢复原始字节。重复请求不会再次补偿。发现外部修改时，保留该文件的现状并继续恢复其他文件。恢复后写审计中断时，重试可接着恢复。 |

## 联调入口

```python
from app.tools.approval import DefaultApprovalService
from app.tools.audit import DefaultAuditService
from app.tools.registry import create_async_builtin_registry
from app.tools.rollback import DefaultRollbackService

tools = create_async_builtin_registry(workspace_path)
approvals = DefaultApprovalService(state_store)
audit = DefaultAuditService(state_store)
rollback = DefaultRollbackService(state_store, checkpoint_store, tools, audit)
```

Agent 主循环在派发敏感工具前请求审批，在写操作前调用 `create_checkpoint`，派发后将 `ToolResult` 完整交给 `StateStore.finish_tool_call`。`ToolResult.to_dict()` 可用于面向模型的结果，补偿凭据留在状态层。工具开始与结束由编排方通过 `audit.log` 写入；审批决定由 `StateStore.decide_approval` 同事务记录。回滚应在所属会话停止执行时调用。

首批补偿覆盖工作区内的文件创建和覆盖。覆盖时恢复原始内容、文件权限和修改时间，创建时删除相应文件。文件写入创建的父目录会保留。回滚前通过内容、权限和修改时间检查外部改动。联调验收时请串行执行文件写入和回滚，避免其他进程在检查与替换之间修改目标文件。补偿凭据含原始文件内容的 Base64 编码，和会话原始数据采用相同的访问边界。
