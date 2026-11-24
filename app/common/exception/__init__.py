"""跨模块异常。实现方只抛这里的类型，Web 层据此映射 HTTP 状态。"""

from app.common.exception.harness_exception import (
    ApprovalError,
    ConfigError,
    ConflictError,
    HarnessError,
    NotFoundError,
    ProviderError,
    StateError,
    SubagentError,
    ToolError,
)

__all__ = [
    "ApprovalError",
    "ConfigError",
    "ConflictError",
    "HarnessError",
    "NotFoundError",
    "ProviderError",
    "StateError",
    "SubagentError",
    "ToolError",
]
