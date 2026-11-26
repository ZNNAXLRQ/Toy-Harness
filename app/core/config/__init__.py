"""配置读取与依赖组装（A 维护）。

- ``settings.py``：``config/config.yaml`` → ``HarnessSettings``（provider profile 解析、
  密钥占位符、校验、路径）。
- ``container.py``：唯一允许 import 全部实现层的装配点（``build_container`` 显式注入、
  ``default_components`` 按约定入口探测实现）。
"""

from app.core.config.container import (
    Components,
    Container,
    build_container,
    build_default_container,
    default_components,
)
from app.core.config.settings import (
    HarnessSettings,
    ProviderProfile,
    ResolvedProvider,
    load_settings,
)

__all__ = [
    "Components",
    "Container",
    "HarnessSettings",
    "ProviderProfile",
    "ResolvedProvider",
    "build_container",
    "build_default_container",
    "default_components",
    "load_settings",
]
