# Nano Codex 第一阶段移植记录

## 范围与来源

以移植时的 `D:/Programs/Python/OOAD/Assignment1/agent.py` 为基准，尽量保留函数主体和循环顺序。
行号为移植时快照，后续应优先按符号名定位。作业原文件未修改；项目不 import 作业目录。

| 目标（均位于 `D:/Programs/Python/OOAD/Project`） | 原来源 | 已实现 | 下一步 |
|---|---|---|---|
| `app/core/agent.py` | Agent/run_agent，245–345 行 | 循环、工具消息、统计、错误、最终回复保存 | StateStore、上下文、异步、安全恢复 |
| `app/core/provider.py` | 客户端及响应解析，227–299 行 | Provider 适配、FakeProvider、配置 | 流式、供应商兼容、预算 |
| `app/core/prompt.py` | SYSTEM_PROMPT，16–40 行 | 先读后改、真实验证、拒绝停止 | AGENTS.md、要求记忆 |
| `app/tools/builtin.py` | 六个文件/进程相关函数，50–127 行 | 五个内置工具、超时、真实输出 | 大文件/输出限制、进程树、补偿 |
| `app/tools/schemas.py` | tools，130–216 行 | 原参数名和 Schema | 插件元数据、版本 |
| `app/tools/registry.py` | 注册/分派，217–225、323–332 行；assessment.py 的接口校验 | 校验、默认拒绝、审批、结果区分 | 持久化状态、幂等、插件 Schema |
| `app/tools/approval.py` | runtime.py:confirm_action，17–37 行 | 拒绝/终端审批适配 | Web 待审批、重启恢复、一次性决策 |
| `app/tools/workspace.py` | Path 操作；assessment.py:linked，22–25 行 | 显式工作区、越界与链接检查 | TOCTOU、操作系统隔离 |
| `app/common/security.py` | runtime.py:redact，10–15 行 | 嵌套事件脱敏 | 敏感字段策略、存储访问控制 |
| `app/common/model/runtime.py`、`app/common/api/runtime.py` | 调用/结果数据与注入点 | Provider、执行、审批、事件接口 | 团队冻结最终契约 |
| `app/common/event/runtime.py` | emit/trace，263–269 行 | 内存事件接收器 | 数据库提交后 SSE 发布 |
| `app/main.py` | main，349–356 行 | 临时 CLI 与离线 Demo | Web 组装层替代 |
| `tests/` | 作业行为与真实执行验证思想 | 自包含离线测试 | SQLite/REST/SSE/恢复验收 |

## 有意保留及改变的行为

1. 保留 argv 列表、`shell=False`、UTF-8 捕获、DEVNULL、15 秒默认超时、当前 Python 解释器。
2. 不使用全局 `os.chdir()`，文件和子进程均绑定显式工作区。
3. 原作业只审批命令；项目将写文件、命令和 Python 执行都作为敏感操作，默认拒绝。
4. 审批与执行紧邻，不向模型提供“自我批准”参数。当前不是可重启恢复的一次性授权系统。
5. 工具结果区分 success/error/denied；非零退出码是 error，不再将“分派成功”等同于业务成功。
6. 拒绝停止整个 turn，并为尚未执行的同批调用补上拒绝结果，防止继续调用另一工具绕过。
7. 最终 assistant 回复写入返回历史；完整历史含原始数据，事件和 CLI 输出另行脱敏。
8. 内置 Schema 校验器仅实现当前使用的 type/properties/required/additionalProperties/items/minItems，
   不能宣称支持任意 JSON Schema。后续插件需要更完整的校验方案。
9. 不移植评分器全局 hook、测试启动恢复、文件夹删除恢复、课程地址和个人凭据。
10. 不把目录哈希快照当成回滚；本阶段没有回滚，也没有 SQLite checkpoint。
11. 当前 approval_result 事件随工具返回后发布，不构成执行前审批的持久化证据。
    Web 接入时必须由审批/执行调度层在执行前保存决定，而不是依赖这个回调补记审计。

## 后续建议顺序

1. A/C/B/D 共同冻结公共接口及状态枚举，尤其是待审批、已派发、已完成、UNKNOWN。
2. C 实现消息/完整 tool_calls/工具结果持久化和会话状态；保存结果与下一检查点必须原子提交。
3. B 将终端审批替换为可持久化的一次性批准，增加工作区策略和写前备份。
4. D 组装 REST/SSE，事件先落库再推送，客户端重连通过 REST 获取权威状态。
5. C/A 接入上下文预算、用户确认要求和安全 Resume；未知外部结果不得自动重放。
6. 最后实现有限本地补偿；不要承诺远程副作用和活动进程的恢复。

本轮没有实现空的 State/Web 模块；未接管其他负责人正在整理的占位文档。