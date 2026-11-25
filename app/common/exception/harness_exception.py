"""Harness 自身的故障类型。

边界约定（所有实现层必须遵守）：

1. **工具的业务失败不是异常。** 命令非零退出、文件不存在、参数不合法，都由
   ``ToolResult(status="error"|"denied")`` 表达，交给模型看见并自行处理。
2. 这里只放 harness 自身坏掉的场景：配置缺失、provider 不可达、状态不一致、
   审批记录缺失、落库失败等。上层不需要 ``try/except`` 区分工具成败。
3. 每个异常带稳定的 ``code``（机器可读）和 ``http_status``（Web 映射），
   Web 层不允许解析 ``message`` 文本判断错误类型。

后续：错误码表随模块扩展追加，但已发布的 code 不改名。
"""

from typing import Any


class HarnessError(Exception):
    """所有 harness 故障的基类。"""

    code = "harness_error"
    http_status = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        """Web 层的统一错误体；不含堆栈，也不含原始凭据。"""
        body: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.detail is not None:
            body["detail"] = self.detail
        return body


class ConfigError(HarnessError):
    """配置缺失或非法（provider 名字、base_url、数据库路径等）。"""

    code = "config_error"


class ProviderError(HarnessError):
    """模型供应商调用失败；SDK 异常必须在此处翻译，不允许外泄。"""

    code = "provider_error"
    http_status = 502


class StateError(HarnessError):
    """会话/消息/工具调用状态不满足不变式（例如同会话出现两个 open turn）。"""

    code = "state_error"


class ApprovalError(HarnessError):
    """审批记录缺失、重复决策、过期决策。"""

    code = "approval_error"
    http_status = 409


class ToolError(HarnessError):
    """工具注册表级别的故障（schema 与签名不一致、重复注册等）。

    工具执行期的业务失败不走这里，见模块开头第 1 条。
    """

    code = "tool_error"


class SubagentError(HarnessError):
    """子代理调度失败：超出并发/深度限制、上下文构建失败。"""

    code = "subagent_error"


class NotFoundError(HarnessError):
    """请求的实体不存在。"""

    code = "not_found"
    http_status = 404


class ConflictError(HarnessError):
    """并发冲突：turn 正在运行、重复恢复、已决策的审批再次提交。"""

    code = "conflict"
    http_status = 409
