"""依赖组装（A 维护）：唯一允许 import 全部实现层的地方。

两条使用方式：

1. ``build_container(settings, components)``：显式传入端口实现（测试、或调用方自己
   装配时用）。所有依赖都是注入的，容器只负责把 ``provider/tools/state/context/
   approval/audit/events/agents/subagent_run_store`` 拼成 ``TurnEngine``、
   ``DefaultAgentLoop`` 与 ``DefaultSubagentScheduler``。
2. ``default_components(settings)``：按**约定的入口**探测各模块的实现（见下），
   缺谁就抛一条点名到人的 ``ConfigError``。这是 B/C/D 的接入契约，路径写死在这里：

| 组件 | 期望入口（实现方） |
|---|---|
| StateStore + SubagentRunStore | `app.state.persistence.factory.create_stores(settings)` → `(state, subagent_run_store)`（C） |
| ContextManager | `app.state.context.factory.create_context_manager(settings)`（C） |
| ToolRegistry | `app.tools.registry.create_async_builtin_registry(workspace)`（B） |
| ApprovalService | `app.tools.approval.DefaultApprovalService(state)`（B） |
| AuditService | `app.tools.audit.DefaultAuditService(state)`（B） |
| EventPublisher | `app.web.sse.sse_event_publisher.SseEventPublisher()`（D） |
| AgentsMdLoader（可选） | `app.state.agents.default_agents_md_loader.DefaultAgentsMdLoader()`（C） |

**Provider 是延迟构造的**：没有配密钥时容器仍能建起来（服务可以启动、浏览历史），
只有第一次真正调模型时才解析配置并可能抛 ``ConfigError``——这比启动即崩更好排查。
"""

import asyncio
from dataclasses import dataclass
from typing import Callable

from app.common.api.agents_md_loader import AgentsMdLoader
from app.common.api.approval_service import ApprovalService
from app.common.api.audit_service import AuditService
from app.common.api.context_manager import ContextManager
from app.common.event.event_publisher import EventPublisher
from app.common.api.llm_provider import CompletionRequest, LLMProvider, ModelResponse
from app.common.api.state_store import StateStore
from app.common.api.subagent_scheduler import SubagentRunStore
from app.common.api.tool_registry import ToolRegistry
from app.common.exception import ConfigError
from app.core.config.settings import HarnessSettings, load_settings
from app.core.loop import DefaultAgentLoop, LoopConfig, TurnEngine
from app.core.provider.provider_factory import create_provider
from app.core.subagent import DefaultSubagentScheduler, DelegationPolicy


class _LazyProvider:
    """第一次 ``complete`` 时才解析配置构造真实 provider。"""

    provider_name = "lazy"

    def __init__(self, factory: Callable[[], LLMProvider]) -> None:
        self._factory = factory
        self._provider: LLMProvider | None = None
        self._lock = asyncio.Lock()

    async def complete(self, request: CompletionRequest) -> ModelResponse:
        if self._provider is None:
            async with self._lock:
                if self._provider is None:
                    self._provider = self._factory()
        assert self._provider is not None
        return await self._provider.complete(request)


@dataclass(frozen=True)
class Components:
    """一次装配所需的全部端口实现。"""

    state: StateStore
    subagent_run_store: SubagentRunStore
    tools: ToolRegistry
    approval: ApprovalService
    context: ContextManager
    events: EventPublisher
    audit: AuditService | None = None
    agents: AgentsMdLoader | None = None


@dataclass
class Container:
    settings: HarnessSettings
    provider: LLMProvider
    components: Components
    engine: TurnEngine
    loop: DefaultAgentLoop
    subagents: DefaultSubagentScheduler

    @property
    def state(self) -> StateStore:
        return self.components.state

    @property
    def tools(self) -> ToolRegistry:
        return self.components.tools

    @property
    def events(self) -> EventPublisher:
        return self.components.events

    def close(self) -> None:
        """对称的关闭钩子；当前实现没有需要释放的常驻资源。"""


def build_engine(
    settings: HarnessSettings,
    components: Components,
    *,
    provider: LLMProvider,
    policy: DelegationPolicy | None = None,
) -> TurnEngine:
    del policy  # 保留参数位，方便将来 loop 级策略（当前只用于子代理）
    return TurnEngine(
        provider=provider,
        tools=components.tools,
        state=components.state,
        context=components.context,
        approval=components.approval,
        events=components.events,
        agents=components.agents,
        config=LoopConfig(
            max_iterations=settings.agent.max_iterations,
            token_budget=settings.agent.token_budget,
            workspace=settings.workspace_path(),
        ),
    )


def build_subagents(
    settings: HarnessSettings,
    components: Components,
    *,
    provider: LLMProvider,
    policy: DelegationPolicy | None = None,
) -> DefaultSubagentScheduler:
    effective = policy or DelegationPolicy(
        max_concurrency=settings.subagent.max_concurrency,
        max_depth=settings.subagent.max_depth,
    )
    return DefaultSubagentScheduler(
        provider=provider,
        tools=components.tools,
        run_store=components.subagent_run_store,
        events=components.events,
        policy=effective,
    )


def build_container(
    settings: HarnessSettings,
    components: Components,
    *,
    provider: LLMProvider | None = None,
    provider_factory: Callable[[], LLMProvider] | None = None,
    policy: DelegationPolicy | None = None,
) -> Container:
    """用显式注入的组件装配整个运行时。"""
    effective_provider = provider
    if effective_provider is None:
        factory = provider_factory or (lambda: create_provider(settings))
        effective_provider = _LazyProvider(factory)
    engine = build_engine(settings, components, provider=effective_provider, policy=policy)
    loop = DefaultAgentLoop(engine, state=components.state, audit=components.audit)
    subagents = build_subagents(
        settings, components, provider=effective_provider, policy=policy
    )
    return Container(
        settings=settings,
        provider=effective_provider,
        components=components,
        engine=engine,
        loop=loop,
        subagents=subagents,
    )


# ---------------------------------------------------------------------------
# 默认组件探测：把「谁还没交付」说清楚，而不是给一个 ImportError 堆栈
# ---------------------------------------------------------------------------


def _missing(name: str, who: str, entry: str) -> ConfigError:
    return ConfigError(
        f"{name} 实现尚未交付（{who}）：需要 {entry}",
        detail={"component": name, "owner": who, "expected": entry},
    )


def default_components(settings: HarnessSettings) -> Components:
    """按约定入口探测实现；缺任何必需组件都汇总成一条 ``ConfigError``。"""
    missing: list[ConfigError] = []

    state: StateStore | None = None
    subagent_run_store: SubagentRunStore | None = None
    try:
        from app.state.persistence.factory import create_stores  # type: ignore[import-not-found]

        state, subagent_run_store = create_stores(settings)
    except ImportError:
        missing.append(
            _missing(
                "StateStore + SubagentRunStore",
                "C",
                "app.state.persistence.factory.create_stores(settings) -> (state, subagent_run_store)",
            )
        )

    context: ContextManager | None = None
    try:
        from app.state.context.factory import create_context_manager  # type: ignore[import-not-found]

        context = create_context_manager(settings)
    except ImportError:
        missing.append(
            _missing(
                "ContextManager",
                "C",
                "app.state.context.factory.create_context_manager(settings)",
            )
        )

    tools: ToolRegistry | None = None
    try:
        from app.tools.registry import create_async_builtin_registry  # type: ignore[attr-defined]

        tools = create_async_builtin_registry(settings.workspace_path())
    except ImportError:
        missing.append(
            _missing(
                "ToolRegistry",
                "B",
                "app.tools.registry.create_async_builtin_registry(workspace) -> ToolRegistry",
            )
        )

    approval: ApprovalService | None = None
    audit: AuditService | None = None
    if state is not None:
        try:
            from app.tools.approval import DefaultApprovalService  # type: ignore[attr-defined]

            approval = DefaultApprovalService(state)
        except ImportError:
            missing.append(
                _missing("ApprovalService", "B", "app.tools.approval.DefaultApprovalService(state)")
            )
        try:
            from app.tools.audit import DefaultAuditService  # type: ignore[attr-defined]

            audit = DefaultAuditService(state)
        except ImportError:
            missing.append(
                _missing("AuditService", "B", "app.tools.audit.DefaultAuditService(state)")
            )

    events: EventPublisher | None = None
    try:
        from app.web.sse.sse_event_publisher import SseEventPublisher  # type: ignore[import-not-found]

        events = SseEventPublisher()
    except ImportError:
        missing.append(
            _missing(
                "EventPublisher",
                "D",
                "app.web.sse.sse_event_publisher.SseEventPublisher()",
            )
        )

    agents: AgentsMdLoader | None = None
    try:
        from app.state.agents.default_agents_md_loader import (  # type: ignore[import-not-found]
            DefaultAgentsMdLoader,
        )

        agents = DefaultAgentsMdLoader()
    except ImportError:
        agents = None  # 可选组件：没有 AGENTS.md 加载器也能跑

    if missing:
        raise ConfigError(
            "容器无法装配：以下实现尚未交付 —— "
            + "；".join(error.message for error in missing),
            detail={"missing": [error.detail for error in missing]},
        )

    assert state is not None and subagent_run_store is not None
    assert context is not None and tools is not None
    assert approval is not None and events is not None
    return Components(
        state=state,
        subagent_run_store=subagent_run_store,
        tools=tools,
        approval=approval,
        context=context,
        events=events,
        audit=audit,
        agents=agents,
    )


def build_default_container(settings: HarnessSettings | None = None) -> Container:
    """探测默认实现并装配（D 的 ``app/main.py`` 用它）。"""
    effective = settings or load_settings()
    return build_container(effective, default_components(effective))
