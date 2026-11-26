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
from typing import Any, Callable

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
# 默认组件探测：把「谁还没交付 / 哪一步装配失败」说清楚，而不是给一个裸异常
# ---------------------------------------------------------------------------

STATE_STORES_ENTRY = (
    "app.state.persistence.factory.create_stores(settings) -> (state, subagent_run_store)"
)
CONTEXT_ENTRY = "app.state.context.factory.create_context_manager(settings)"
TOOLS_ENTRY = "app.tools.registry.create_async_builtin_registry(workspace) -> ToolRegistry"
APPROVAL_ENTRY = "app.tools.approval.DefaultApprovalService(state)"
AUDIT_ENTRY = "app.tools.audit.DefaultAuditService(state)"
EVENTS_ENTRY = "app.web.sse.sse_event_publisher.SseEventPublisher()"
AGENTS_ENTRY = "app.state.agents.default_agents_md_loader.DefaultAgentsMdLoader()"


def _missing(name: str, who: str, entry: str, reason: str) -> ConfigError:
    return ConfigError(
        f"{name} 装配失败（{who}）：{reason}；需要 {entry}",
        detail={"component": name, "owner": who, "expected": entry, "reason": reason},
    )


def _probe(name: str, who: str, entry: str, loader: Callable[[], Any]) -> Any:
    """把「未交付」和「交付了但构造失败」都翻译成点名 ConfigError。"""
    try:
        return loader()
    except ImportError as error:
        raise _missing(name, who, entry, f"尚未交付（{error}）") from error
    except ConfigError:
        raise
    except Exception as error:  # noqa: BLE001 - 构造期任何异常都要可读
        raise _missing(
            name, who, entry, f"构造时出错：{type(error).__name__}: {error}"
        ) from error


# 下面这些函数是**可被测试替换**的探测点（tests/test_container.py 用 monkeypatch 模拟
# 缺 C / 缺 B / 缺 D，而不是假设"当前全部未交付"）。
def _load_state_stores(settings: HarnessSettings) -> tuple[StateStore, SubagentRunStore]:
    from app.state.persistence.factory import create_stores  # type: ignore[import-not-found]

    return create_stores(settings)


def _load_context(settings: HarnessSettings) -> ContextManager:
    from app.state.context.factory import create_context_manager  # type: ignore[import-not-found]

    return create_context_manager(settings)


def _load_tools(settings: HarnessSettings) -> ToolRegistry:
    from app.tools.registry import create_async_builtin_registry  # type: ignore[attr-defined]

    return create_async_builtin_registry(settings.workspace_path())


def _load_approval(state: StateStore) -> ApprovalService:
    from app.tools.approval import DefaultApprovalService  # type: ignore[attr-defined]

    return DefaultApprovalService(state)


def _load_audit(state: StateStore) -> AuditService:
    from app.tools.audit import DefaultAuditService  # type: ignore[attr-defined]

    return DefaultAuditService(state)


def _load_events() -> EventPublisher:
    from app.web.sse.sse_event_publisher import SseEventPublisher  # type: ignore[import-not-found]

    return SseEventPublisher()


def _load_agents() -> AgentsMdLoader:
    from app.state.agents.default_agents_md_loader import (  # type: ignore[import-not-found]
        DefaultAgentsMdLoader,
    )

    return DefaultAgentsMdLoader()


def default_components(settings: HarnessSettings) -> Components:
    """按约定入口探测实现；缺任何必需组件都汇总成一条 ``ConfigError``。"""
    errors: list[ConfigError] = []

    def attempt(name: str, who: str, entry: str, loader: Callable[[], Any]) -> Any:
        try:
            return _probe(name, who, entry, loader)
        except ConfigError as error:
            errors.append(error)
            return None

    stores = attempt(
        "StateStore + SubagentRunStore",
        "C",
        STATE_STORES_ENTRY,
        lambda: _load_state_stores(settings),
    )
    context = attempt(
        "ContextManager", "C", CONTEXT_ENTRY, lambda: _load_context(settings)
    )
    tools = attempt("ToolRegistry", "B", TOOLS_ENTRY, lambda: _load_tools(settings))
    events = attempt("EventPublisher", "D", EVENTS_ENTRY, _load_events)

    approval = audit = None
    if stores is not None:
        state, subagent_run_store = stores
        approval = attempt(
            "ApprovalService", "B", APPROVAL_ENTRY, lambda: _load_approval(state)
        )
        audit = attempt("AuditService", "B", AUDIT_ENTRY, lambda: _load_audit(state))

    # AGENTS.md 加载器是可选组件：没有它 loop 只是不读 AGENTS.md。
    agents = None
    try:
        agents = _load_agents()
    except Exception:  # noqa: BLE001 - 可选组件，未交付/构造失败都退化为 None
        agents = None

    if errors:
        raise ConfigError(
            "容器无法装配：以下实现未就绪 —— " + "；".join(error.message for error in errors),
            detail={"missing": [error.detail for error in errors]},
        )

    assert stores is not None and context is not None
    assert tools is not None and events is not None
    assert approval is not None
    state, subagent_run_store = stores
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