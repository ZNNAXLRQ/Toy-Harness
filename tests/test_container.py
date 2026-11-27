"""容器装配测试：显式组件、惰性 provider、默认探测的报错信息。"""

import pytest

from app.common.api.llm_provider import ModelResponse
from app.common.api.subagent_scheduler import SubagentTask
from app.common.exception import ConfigError
from app.core.config import settings as settings_module
from app.core.config.container import (
    Components,
    build_container,
    default_components,
)
from app.core.config.settings import HarnessSettings
from app.core.subagent import DefaultSubagentScheduler
from app.core.loop import DefaultAgentLoop
from tests.fakes.fake_context_events import PassThroughContext, RecordingEventPublisher
from tests.fakes.fake_subagent_store import MemorySubagentRunStore
from tests.fakes.fake_tools import FakeApprovalService, FakeToolRegistry
from tests.fakes.memory_audit_service import MemoryAuditService
from tests.fakes.memory_state_store import MemoryStateStore


def make_components():
    state = MemoryStateStore()
    tools = FakeToolRegistry()
    tools.add("read_file", lambda arguments: "content")
    tools.add("list_files", lambda arguments: ["content"])
    return Components(
        state=state,
        subagent_run_store=MemorySubagentRunStore(),
        tools=tools,
        approval=FakeApprovalService(state),
        context=PassThroughContext(),
        events=RecordingEventPublisher(),
        audit=MemoryAuditService(state),
    )


async def test_build_container_wires_loop_and_subagents():
    settings = HarnessSettings()
    components = make_components()
    provider = _scripted([ModelResponse(content="hello")])

    container = build_container(settings, components, provider=provider)
    session = await components.state.create_session()

    outcome = await container.loop.run_turn(session.id, user_content="hi")

    assert outcome.status.value == "completed"
    assert outcome.content == "hello"
    assert isinstance(container.loop, DefaultAgentLoop)
    assert isinstance(container.subagents, DefaultSubagentScheduler)
    assert container.subagents.policy.max_depth == settings.subagent.max_depth
    assert container.subagents.policy.max_concurrency == settings.subagent.max_concurrency
    assert container.state is components.state


async def test_container_builds_a_working_subagent_scheduler():
    settings = HarnessSettings()
    components = make_components()
    provider = _scripted([ModelResponse(content="subtask done")])
    container = build_container(settings, components, provider=provider)

    result = await container.subagents.spawn(
        SubagentTask(
            id="t1",
            parent_session_id="s1",
            parent_call_id="c1",
            prompt="read it",
        )
    )

    assert result.status == "success"
    assert result.summary == "subtask done"
    rows = await components.subagent_run_store.list_runs("s1")
    assert len(rows) == 1 and rows[0].status == "success"


async def test_lazy_provider_defers_config_errors_to_the_first_model_call(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings_module, "CONFIG_FILE", config)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    settings = HarnessSettings()
    components = make_components()

    container = build_container(settings, components)  # 没有密钥也应该能装配
    session = await components.state.create_session()
    outcome = await container.loop.run_turn(session.id, user_content="hi")

    assert outcome.status.value == "failed"
    assert "ConfigError" in (outcome.error or "")


async def test_provider_factory_is_injectable():
    settings = HarnessSettings()
    components = make_components()
    calls = []

    def factory():
        calls.append(1)
        return _scripted([ModelResponse(content="from factory")])

    container = build_container(settings, components, provider_factory=factory)
    session = await components.state.create_session()
    outcome = await container.loop.run_turn(session.id, user_content="hi")

    assert outcome.content == "from factory"
    assert calls == [1]


def test_default_components_aggregates_missing_components(monkeypatch):
    """缺多个组件时汇总成一条点名到人的 ConfigError，而不是裸 ImportError。"""
    import app.core.config.container as container_module

    def missing(*args, **kwargs):
        raise ImportError("not delivered")

    for name in (
        "_load_state_stores",
        "_load_context",
        "_load_tools",
        "_load_events",
    ):
        monkeypatch.setattr(container_module, name, missing)

    with pytest.raises(ConfigError) as caught:
        default_components(HarnessSettings())

    message = caught.value.message
    assert "app.state.persistence.factory" in message  # C
    assert "create_async_builtin_registry" in message  # B
    assert "SseEventPublisher" in message  # D
    missing = caught.value.detail["missing"]
    assert {item["owner"] for item in missing} == {"B", "C", "D"}
    assert len(missing) == 4  # state+subagent / context / tools / events


def test_default_components_wraps_construction_errors(monkeypatch):
    """交付了但构造失败（例如 workspace 不存在）也要翻译成点名 ConfigError。"""
    import app.core.config.container as container_module

    def missing(*args, **kwargs):
        raise ImportError("not delivered")

    for name in ("_load_state_stores", "_load_context", "_load_events"):
        monkeypatch.setattr(container_module, name, missing)

    def broken_tools(settings):
        raise FileNotFoundError("workspace directory is missing")

    monkeypatch.setattr(container_module, "_load_tools", broken_tools)

    with pytest.raises(ConfigError) as caught:
        default_components(HarnessSettings())

    message = caught.value.message
    assert "ToolRegistry" in message
    assert "FileNotFoundError" in message
    assert "workspace directory is missing" in message


async def test_default_components_assembles_when_all_probes_succeed(monkeypatch):
    import app.core.config.container as container_module

    state = MemoryStateStore()
    subagent_store = MemorySubagentRunStore()
    tools = FakeToolRegistry()
    tools.add("read_file", lambda arguments: "content")
    events = RecordingEventPublisher()

    monkeypatch.setattr(container_module, "_load_state_stores", lambda settings: (state, subagent_store))
    monkeypatch.setattr(container_module, "_load_context", lambda settings: PassThroughContext())
    monkeypatch.setattr(container_module, "_load_tools", lambda settings: tools)
    monkeypatch.setattr(container_module, "_load_approval", lambda store: FakeApprovalService(store))
    monkeypatch.setattr(container_module, "_load_audit", lambda store: MemoryAuditService(store))
    monkeypatch.setattr(container_module, "_load_events", lambda: events)
    monkeypatch.setattr(container_module, "_load_agents", lambda: None)

    components = default_components(HarnessSettings())
    provider = _scripted([ModelResponse(content="wired")])
    container = build_container(HarnessSettings(), components, provider=provider)
    session = await state.create_session()

    outcome = await container.loop.run_turn(session.id, user_content="hi")

    assert components.state is state
    assert components.events is events
    assert outcome.content == "wired"




class _ScriptedProvider:
    provider_name = "scripted"

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return self._responses.pop(0)


def _scripted(responses):
    return _ScriptedProvider(responses)
