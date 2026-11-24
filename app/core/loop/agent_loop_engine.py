"""Agent 主循环引擎：模型 ↔ 工具的状态机（不含锁、不含 HTTP）。

调用方只看到 ``run`` / ``resume``；一轮内部的顺序固定为：

    (挂起恢复) → 有未终态调用就逐个执行 → 没有就调模型
              → 模型要工具就落库并执行 → 模型给最终回答就结束

三条硬约束（来自契约层的评审）：

1. **审批挂起不阻塞**：敏感调用先 ``record_approval_request`` 落库、把会话置
   ``waiting_approval``，然后 **返回**；恢复时先校验审批记录再消费。
2. **派发只有一次**：执行前必须 ``claim_tool_call`` 抢到 CAS；抢不到就抛
   ``ConflictError``，绝不重试执行。
3. **事件是 best-effort**：推送失败不影响本轮结果；但事件对象本身仍然要先构造，
   构造期的 payload 校验错误是代码 bug，不能被吞掉。

工具失败（坏 JSON、未知工具、执行异常）一律折叠成 ``ToolResult`` 观察结果喂回模型；
只有「拒绝」会终止整轮，防止模型绕开审批。
"""

import hashlib
import json
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.common.api.agent_loop import TurnOutcome
from app.common.api.agents_md_loader import AgentsMdLoader
from app.common.api.approval_service import ApprovalService
from app.common.api.context_manager import ContextManager
from app.common.api.llm_provider import CompletionRequest, LLMProvider
from app.common.api.state_store import (
    TERMINAL_SESSION_STATUSES,
    ApprovalStatus,
    MessageRole,
    SessionStatus,
    StateStore,
    ToolCallStatus,
    TurnStatus,
)
from app.common.api.tool import ToolResult
from app.common.api.tool_registry import ToolRegistry
from app.common.event.agent_event import AgentEvent
from app.common.event.event_publisher import EventPublisher
from app.common.event.event_type import EventType
from app.common.exception import (
    ConflictError,
    HarnessError,
    NotFoundError,
    ProviderError,
    StateError,
    ToolError,
)
from app.common.security import redact
from app.core.loop.prompt import SYSTEM_PROMPT
from app.core.loop.turn_state import TurnProgress

_DENIED_TURN_MESSAGE = "Operation refused; the turn was stopped without using another tool."
_TERMINAL = {status.value for status in TERMINAL_SESSION_STATUSES}


@dataclass(frozen=True)
class LoopConfig:
    """一轮对话的策略参数（来自 ``config.yaml`` 的 agent 段）。"""

    max_iterations: int = 15
    token_budget: int = 32000
    system_prompt: str = SYSTEM_PROMPT
    workspace: Path | None = None

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be a positive integer")
        if self.token_budget < 1:
            raise ValueError("token_budget must be positive")


class TurnEngine:
    def __init__(
        self,
        *,
        provider: LLMProvider,
        tools: ToolRegistry,
        state: StateStore,
        context: ContextManager,
        approval: ApprovalService,
        events: EventPublisher,
        agents: AgentsMdLoader | None = None,
        config: LoopConfig | None = None,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.state = state
        self.context = context
        self.approval = approval
        self.events = events
        self.agents = agents
        self.config = config or LoopConfig()

    # ------------------------------------------------------------------ public

    async def run(
        self, session_id: str, *, user_content: str | None = None
    ) -> TurnOutcome:
        """执行或恢复一轮（``user_content=None`` 表示恢复）。"""
        session = await self._load_active_session(session_id)
        progress = TurnProgress(session_id=session_id)

        if user_content is not None:
            message = await self.state.append_message(
                session_id, MessageRole.USER, user_content
            )
            await self._set_status(session_id, SessionStatus.OPEN)
            await self.publish(
                EventType.USER_MESSAGE,
                session_id,
                seq=message.seq,
                message_id=message.id,
                content=user_content,
            )
        elif session.status == SessionStatus.WAITING_APPROVAL.value:
            return await self.resume(session_id)

        return await self._drive(session_id, progress)

    async def resume(self, session_id: str, *, approval_id: str | None = None) -> TurnOutcome:
        """消费已落库的审批决定并继续。"""
        await self._load_active_session(session_id)
        turn = await self.state.load_open_turn(session_id)
        progress = TurnProgress(session_id=session_id)

        if turn is None:
            raise ConflictError(f"session {session_id} has no open turn to resume")
        if turn.status == TurnStatus.FAILED:
            return await self._fail_unknown(session_id, progress, turn)

        if turn.status == TurnStatus.WAITING_APPROVAL:
            if turn.pending_call_id is None:
                raise ConflictError(f"session {session_id} is waiting but has no pending call")
            approval = await self.approval.find_for_call(turn.pending_call_id)
            if approval is None:
                raise ConflictError(
                    f"tool call {turn.pending_call_id} is awaiting approval but has no record"
                )
            if approval_id is not None and approval.id != approval_id:
                raise ConflictError(
                    f"approval {approval_id} does not belong to pending call {turn.pending_call_id}"
                )
            if approval.status == ApprovalStatus.PENDING.value:
                raise ConflictError(f"approval {approval.id} is not decided yet")
            if approval.status == ApprovalStatus.DENIED.value:
                return await self._deny_pending_turn(session_id, turn.pending_call_id, progress)
            if not await self.state.claim_tool_call(
                turn.pending_call_id,
                from_status=ToolCallStatus.AWAITING_APPROVAL,
                to_status=ToolCallStatus.APPROVED,
            ):
                raise ConflictError(
                    f"approval for {turn.pending_call_id} was consumed by another resumer"
                )
            await self.publish(
                EventType.APPROVAL_DECIDED,
                session_id,
                approval_id=approval.id,
                call_id=turn.pending_call_id,
                decision=approval.status,
                actor=approval.decided_by or "system",
            )
            await self._set_status(session_id, SessionStatus.OPEN)

        return await self._drive(session_id, progress)

    async def publish(self, event_type: EventType, session_id: str, *, seq: int = 0, **payload: Any) -> None:
        """构造并发布事件；**只吞发布失败**，构造期的契约错误照常抛出。"""
        event = AgentEvent(
            type=event_type, session_id=session_id, seq=seq, payload=payload
        )
        try:
            await self.events.publish(event)
        except Exception:  # noqa: BLE001 - 事件是 best-effort，不允许弄崩一轮
            return

    # ---------------------------------------------------------------- driving

    async def _drive(self, session_id: str, progress: TurnProgress) -> TurnOutcome:
        try:
            while True:
                turn = await self.state.load_open_turn(session_id)
                if turn is None:
                    return await self._complete(session_id, progress)
                if turn.status == TurnStatus.FAILED:
                    return await self._fail_unknown(session_id, progress, turn)
                if turn.open_call_ids:
                    outcome = await self._run_open_calls(session_id, progress)
                    if outcome is not None:
                        return outcome
                    continue
                if progress.iteration >= self.config.max_iterations:
                    return await self._fail(
                        session_id, progress, "Maximum iterations reached without completion"
                    )
                outcome = await self._model_step(session_id, progress)
                if outcome is not None:
                    return outcome
        except ProviderError as error:
            return await self._fail(session_id, progress, f"ProviderError: {error.message}")
        except HarnessError as error:
            return await self._fail(
                session_id, progress, f"{type(error).__name__}: {error.message}"
            )
        except Exception as error:  # noqa: BLE001 - 兜底：任何意外都要让 turn 收敛
            return await self._fail(
                session_id, progress, redact(f"{type(error).__name__}: {error}")
            )

    async def _model_step(self, session_id: str, progress: TurnProgress) -> TurnOutcome | None:
        prefix, agents_version = self._build_prefix()
        await self._sync_prefix(session_id, prefix, agents_version)

        wire_messages = await self._wire_messages(session_id)
        if self.context.should_compact():
            summary = await self.context.build_summary(session_id, wire_messages)
            if summary:
                await self.state.append_message(session_id, MessageRole.SUMMARY, summary)
                wire_messages = await self._wire_messages(session_id)

        prepared = await self.context.prepare(
            session_id, prefix, wire_messages, budget=self.config.token_budget
        )
        response = await self.provider.complete(
            CompletionRequest(
                prefix=prepared.prefix,
                messages=prepared.messages,
                tools=list(self.tools.schemas),
            )
        )
        progress.record_response(response)
        self.context.record_usage(response.tokens)
        await self.publish(
            EventType.MODEL_RESPONSE,
            session_id,
            iteration=progress.iteration,
            content=response.content or "",
            finish_reason=response.finish_reason or "",
            tool_calls=[
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in response.tool_calls
            ],
        )

        if response.finish_reason in {"length", "content_filter"}:
            return await self._fail(session_id, progress, "Incomplete model response")

        try:
            await self.state.record_assistant_turn(session_id, response)
        except StateError as error:
            return await self._fail(session_id, progress, f"StateError: {error.message}")

        if not response.tool_calls:
            return await self._complete(session_id, progress, response.content)
        return None

    async def _run_open_calls(
        self, session_id: str, progress: TurnProgress
    ) -> TurnOutcome | None:
        await self._set_status(session_id, SessionStatus.OPEN)
        turn = await self.state.load_open_turn(session_id)
        if turn is None:
            return await self._complete(session_id, progress)

        calls = {call.id: call for call in await self.state.list_tool_calls(session_id)}
        for call_id in turn.open_call_ids:
            call = calls.get(call_id)
            if call is None:
                continue
            status = ToolCallStatus(call.status)

            if status == ToolCallStatus.APPROVED:
                if not await self.state.claim_tool_call(
                    call_id,
                    from_status=ToolCallStatus.APPROVED,
                    to_status=ToolCallStatus.DISPATCHED,
                ):
                    raise ConflictError(f"tool call {call_id} was already dispatched elsewhere")
                arguments = self._parse_arguments(call.arguments)
                if isinstance(arguments, ToolResult):
                    await self._finish_and_publish(session_id, call, arguments, started=time.monotonic())
                    continue
                outcome = await self._execute(session_id, call, arguments, progress)
                if outcome is not None:
                    return outcome
                continue

            # PENDING：先解析参数、校验工具，再决定要不要审批
            arguments = self._parse_arguments(call.arguments)
            if isinstance(arguments, ToolResult):
                await self._finish_and_publish(session_id, call, arguments, started=time.monotonic())
                continue
            try:
                self.tools.get(call.tool_name)
            except ToolError as error:
                await self._finish_and_publish(
                    session_id,
                    call,
                    ToolResult("error", f"Tool error ({type(error).__name__}): {error.message}"),
                    started=time.monotonic(),
                )
                continue

            if self.approval.requires_approval(call.tool_name, arguments):
                approval = await self.state.record_approval_request(
                    session_id,
                    call.id,
                    call.tool_name,
                    call.arguments or "{}",
                    f"{call.tool_name}: {json.dumps(arguments, ensure_ascii=False)}",
                )
                await self._set_status(session_id, SessionStatus.WAITING_APPROVAL)
                await self.publish(
                    EventType.APPROVAL_REQUIRED,
                    session_id,
                    approval_id=approval.id,
                    call_id=call.id,
                    tool_name=call.tool_name,
                    arguments=arguments,
                    description=approval.description or "",
                )
                return TurnOutcome(
                    session_id=session_id,
                    status=TurnStatus.WAITING_APPROVAL,
                    pending_call_id=call.id,
                    iterations=progress.iteration,
                    tokens=progress.tokens,
                )

            if not await self.state.claim_tool_call(
                call_id,
                from_status=ToolCallStatus.PENDING,
                to_status=ToolCallStatus.DISPATCHED,
            ):
                raise ConflictError(f"tool call {call_id} was already claimed")
            outcome = await self._execute(session_id, call, arguments, progress)
            if outcome is not None:
                return outcome
        return None

    async def _execute(
        self, session_id: str, call: Any, arguments: dict[str, Any], progress: TurnProgress
    ) -> TurnOutcome | None:
        started = time.monotonic()
        await self.publish(
            EventType.TOOL_START,
            session_id,
            iteration=progress.iteration,
            call_id=call.id,
            name=call.tool_name,
            arguments=arguments,
        )
        try:
            result = await self.tools.execute(call.id, call.tool_name, arguments)
        except ToolError as error:
            result = ToolResult("error", f"Tool error ({type(error).__name__}): {error.message}")
        except Exception as error:  # noqa: BLE001 - 工具异常折叠成观察结果
            result = ToolResult("error", redact(f"Tool error ({type(error).__name__}): {error}"))

        await self._finish_and_publish(session_id, call, result, started=started)
        if result.status == "denied":
            return await self._deny_remaining(session_id, progress)
        return None

    async def _finish_and_publish(
        self, session_id: str, call: Any, result: ToolResult, *, started: float
    ) -> None:
        await self.state.finish_tool_call(call.id, result)
        await self.publish(
            EventType.TOOL_END,
            session_id,
            call_id=call.id,
            name=call.tool_name,
            status=result.status,
            result=result.to_dict().get("result"),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    # ------------------------------------------------------------- termination

    async def _complete(
        self, session_id: str, progress: TurnProgress, content: str | None = None
    ) -> TurnOutcome:
        final = content if content is not None else progress.last_content
        await self._set_status(session_id, SessionStatus.COMPLETED)
        if progress.iteration:
            await self.publish(
                EventType.TURN_COMPLETED,
                session_id,
                iterations=progress.iteration,
                tokens=progress.tokens,
                content=final or "",
            )
        return TurnOutcome(
            session_id=session_id,
            status=TurnStatus.COMPLETED,
            content=final,
            iterations=progress.iteration,
            tokens=progress.tokens,
        )

    async def _fail(
        self, session_id: str, progress: TurnProgress, error: str
    ) -> TurnOutcome:
        message = redact(error)
        await self._set_status(session_id, SessionStatus.FAILED)
        await self.publish(
            EventType.TURN_FAILED, session_id, error=message, unknown_calls=[]
        )
        return TurnOutcome(
            session_id=session_id,
            status=TurnStatus.FAILED,
            iterations=progress.iteration,
            tokens=progress.tokens,
            error=message,
        )

    async def _fail_unknown(
        self, session_id: str, progress: TurnProgress, turn: Any
    ) -> TurnOutcome:
        unknown = list(turn.notes.get("unknown_calls") or [])
        error = (
            "Tool result unknown after restart: "
            + ", ".join(unknown)
            + "; manual confirmation required"
        ) if unknown else "Turn is in a failed state; manual confirmation required"
        await self.publish(
            EventType.TURN_FAILED, session_id, error=error, unknown_calls=unknown
        )
        return TurnOutcome(
            session_id=session_id,
            status=TurnStatus.FAILED,
            iterations=progress.iteration,
            tokens=progress.tokens,
            error=error,
        )

    async def _deny_pending_turn(
        self, session_id: str, call_id: str, progress: TurnProgress
    ) -> TurnOutcome:
        calls = {call.id: call for call in await self.state.list_tool_calls(session_id)}
        call = calls.get(call_id)
        if call is not None:
            result = ToolResult("denied", "Operation denied by the approver.")
            await self._finish_and_publish(
                session_id, call, result, started=time.monotonic()
            )
        return await self._deny_remaining(session_id, progress)

    async def _deny_remaining(
        self, session_id: str, progress: TurnProgress
    ) -> TurnOutcome:
        """拒绝即终止：同批未执行的调用补 denied 结果，整轮失败。"""
        skipped = ToolResult(
            "denied", "Skipped because an earlier operation was refused."
        )
        for call in await self.state.list_tool_calls(session_id):
            status = ToolCallStatus(call.status)
            if status not in {ToolCallStatus.PENDING, ToolCallStatus.AWAITING_APPROVAL}:
                continue
            try:
                await self.state.finish_tool_call(call.id, skipped)
            except (StateError, ConflictError):
                continue
            await self.publish(
                EventType.TOOL_SKIPPED,
                session_id,
                call_id=call.id,
                name=call.tool_name,
                reason=skipped.data,
            )
        await self._set_status(session_id, SessionStatus.FAILED)
        await self.publish(
            EventType.TURN_FAILED, session_id, error="Tool approval denied", unknown_calls=[]
        )
        return TurnOutcome(
            session_id=session_id,
            status=TurnStatus.FAILED,
            content=_DENIED_TURN_MESSAGE,
            iterations=progress.iteration,
            tokens=progress.tokens,
            error="Tool approval denied",
        )

    # ------------------------------------------------------------------ helpers

    async def _load_active_session(self, session_id: str):
        session = await self.state.load_session(session_id)
        if session is None:
            raise NotFoundError(f"session {session_id} not found")
        if session.status in _TERMINAL:
            raise ConflictError(
                f"session {session_id} is {session.status}; start a new turn instead"
            )
        return session

    async def _set_status(self, session_id: str, status: SessionStatus) -> None:
        try:
            await self.state.update_session(session_id, status=status.value)
        except StateError:
            # 会话已经进入硬终态（例如并发恢复把它判了 failed）：不再覆盖。
            return

    def _build_prefix(self) -> tuple[list[dict[str, Any]], str | None]:
        system = self.config.system_prompt
        version: str | None = None
        if self.agents is not None and self.config.workspace is not None:
            document = self.agents.load(self.config.workspace)
            version = document.version
            if document.content:
                system = f"{system}\n\n# AGENTS.md\n{document.content}"
        return [{"role": "system", "content": system}], version

    async def _sync_prefix(
        self, session_id: str, prefix: list[dict[str, Any]], version: str | None
    ) -> None:
        """记录前缀哈希：prefix 变了（system 提示/AGENTS.md/工具集）缓存就失效。"""
        digest = hashlib.sha256()
        digest.update(json.dumps(prefix, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        digest.update(
            json.dumps(
                [dict(tool) for tool in self.tools.schemas],
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        )
        prefix_hash = digest.hexdigest()[:16]
        session = await self.state.load_session(session_id)
        if session is None:
            raise NotFoundError(f"session {session_id} not found")
        if session.prefix_hash != prefix_hash or session.agents_md_version != version:
            await self.state.update_session(
                session_id, prefix_hash=prefix_hash, agents_md_version=version
            )

    async def _wire_messages(self, session_id: str) -> list[dict[str, Any]]:
        messages = await self.state.list_messages(session_id)
        calls_by_message: dict[str, list[Any]] = defaultdict(list)
        for call in await self.state.list_tool_calls(session_id):
            if call.message_id:
                calls_by_message[call.message_id].append(call)

        wire: list[dict[str, Any]] = []
        for message in messages:
            role = message.role
            if role == MessageRole.TOOL.value:
                wire.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id or "",
                        "content": message.content or "",
                    }
                )
            elif role == MessageRole.ASSISTANT.value:
                item: dict[str, Any] = {"role": "assistant", "content": message.content}
                calls = calls_by_message.get(message.id, [])
                if calls:
                    item["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.tool_name,
                                "arguments": call.arguments or "{}",
                            },
                        }
                        for call in calls
                    ]
                wire.append(item)
            elif role == MessageRole.SUMMARY.value:
                wire.append(
                    {"role": "user", "content": f"[历史摘要] {message.content or ''}"}
                )
            elif role == MessageRole.SYSTEM.value:
                continue  # 系统提示在前缀里，不重复
            else:
                wire.append({"role": "user", "content": message.content or ""})
        return wire

    @staticmethod
    def _parse_arguments(raw: str | None) -> dict[str, Any] | ToolResult:
        try:
            arguments = json.loads(raw or "{}")
        except (TypeError, ValueError) as error:
            return ToolResult("error", f"Tool error ({type(error).__name__}): {error}")
        if not isinstance(arguments, dict):
            return ToolResult("error", "Tool error (TypeError): Tool arguments must be a JSON object")
        return arguments
