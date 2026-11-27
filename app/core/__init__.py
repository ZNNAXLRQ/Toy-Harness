"""Core：主循环、模型适配、子代理调度与依赖组装（A 维护）。

- ``loop/``：Agent 主循环（引擎 + 默认实现 + 单轮进度）
- ``provider/``：各模型 SDK 适配（OpenAI/DeepSeek/Claude）与工厂
- ``subagent/``：子代理调度（PR3）
- ``config/``：配置读取与容器（容器在 PR3）
"""
