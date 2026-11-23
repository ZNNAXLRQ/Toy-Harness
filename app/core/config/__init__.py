"""依赖组装（A 维护）。

规则：只有本包可以 import 所有实现层；其他模块只能依赖 ``app/common/api`` 的端口。
"""

from app.core.config.settings import HarnessSettings, load_settings

__all__ = ["HarnessSettings", "load_settings"]
