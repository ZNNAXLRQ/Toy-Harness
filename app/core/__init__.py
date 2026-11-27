"""Core：主循环、模型适配、子代理调度与依赖组装（A 维护）。

- ``loop/``：Agent 主循环（引擎 + 默认实现 + 单轮进度 + 系统提示）
- ``provider/``：各模型 SDK 适配（OpenAI/DeepSeek/Claude）与工厂
- ``subagent/``：子代理调度（白名单/深度/并发/预算、独立上下文、结果回流）
- ``config/``：配置读取（settings）与依赖组装（container：唯一装配点）
"""
