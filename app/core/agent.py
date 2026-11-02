# 来源：D:/Programs/Python/OOAD/Assignment1/agent.py:Agent.run_agent（245–345 行）。
# 功能：保留模型—工具循环、JSON 参数、消息关联、事件和统计；改用公共接口并返回完整历史。
# 后续：StateStore、上下文裁剪、持久化审批、UNKNOWN/Resume 和异步调度；当前只在内存中同步运行。
# 当前 approval_result 随工具返回后发布，不是执行前落库的审批凭据，不能用作持久化审计证据。
import json
import time
from uuid import uuid4

from app.common.api.runtime import EventSink, LLMProvider, ToolExecutor
from app.common.model.runtime import ToolResult
from app.common.security import redact, redact_value
from app.core.prompt import SYSTEM_PROMPT


class Agent:
    def __init__(self, provider: LLMProvider, tools: ToolExecutor, system_prompt=SYSTEM_PROMPT,
                 event_sink: EventSink | None = None, verbose=False):
        self.provider, self.tools = provider, tools
        self.system_prompt, self.event_sink, self.verbose = system_prompt, event_sink, verbose

    def run_agent(self, user_query, max_iterations=15):
        messages = [{"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_query}]
        trace, total_tokens, calls, iteration = [], 0, 0, 0
        run_id, used_ids = uuid4().hex, set()

        def emit(kind, **details):
            event = redact_value({"seq": len(trace) + 1, "run_id": run_id,
                                  "timestamp": time.time(), "kind": kind, **details})
            trace.append(event)
            if self.event_sink is not None:
                self.event_sink.publish(event)
            if self.verbose:
                print(json.dumps(event, ensure_ascii=False), flush=True)

        def finish(success, response, error=None):
            try:
                emit("agent_end", success=success, content=response, error=error)
            except Exception as sink_error:
                success = False
                error = redact(f"Event delivery failed: {type(sink_error).__name__}: {sink_error}")
            return {"success": success, "response": response, "tokens": total_tokens,
                    "tool_calls": calls, "iterations": iteration, "trace": trace,
                    "messages": messages, "run_id": run_id, "error": error}

        try:
            if not isinstance(max_iterations, int) or isinstance(max_iterations, bool) or max_iterations < 1:
                raise ValueError("max_iterations must be a positive integer")
            emit("user_query", content=user_query)
            for iteration in range(1, max_iterations + 1):
                response = self.provider.complete(messages, self.tools.schemas)
                total_tokens += response.tokens
                serial_calls = [call.to_message() for call in response.tool_calls]
                emit("model_response", iteration=iteration, content=response.content,
                     finish_reason=response.finish_reason, tool_calls=serial_calls)
                if response.finish_reason in {"length", "content_filter"}:
                    return finish(False, response.content or "", "Incomplete model response")
                if not response.tool_calls:
                    messages.append({"role": "assistant", "content": response.content or ""})
                    return finish(True, response.content or "")
                ids = [call.id for call in response.tool_calls]
                if any(not identifier or identifier in used_ids for identifier in ids) or len(set(ids)) != len(ids):
                    raise ValueError("Tool call IDs must be non-empty and unique within a run")
                used_ids.update(ids)
                assistant = {"role": "assistant", "content": response.content, "tool_calls": serial_calls}
                if response.reasoning_content is not None:
                    assistant["reasoning_content"] = response.reasoning_content
                messages.append(assistant)
                denied = False
                for call in response.tool_calls:
                    if denied:
                        result = ToolResult("denied", "Skipped because an earlier operation was refused.", False)
                        emit("tool_skipped", call_id=call.id, name=call.name, status=result.status)
                    else:
                        calls += 1
                        started = time.monotonic()
                        arguments = None
                        emit("tool_start", iteration=iteration, call_id=call.id,
                             name=call.name, raw_arguments=call.arguments)
                        try:
                            arguments = json.loads(call.arguments)
                            if not isinstance(arguments, dict):
                                raise TypeError("Tool arguments must be a JSON object")
                            result = self.tools.execute(call.id, call.name, arguments)
                        except Exception as error:
                            result = ToolResult("error", f"Tool error ({type(error).__name__}): {error}")
                        if result.approved is not None:
                            emit("approval_result", call_id=call.id, name=call.name, approved=result.approved)
                        emit("tool_end", call_id=call.id, name=call.name, arguments=arguments,
                             status=result.status, result=result.to_dict(),
                             elapsed_seconds=round(time.monotonic() - started, 4))
                        denied = result.status == "denied"
                    messages.append({"role": "tool", "content": result.to_content(), "tool_call_id": call.id})
                if denied:
                    return finish(False, "Operation refused; the turn was stopped without using another tool.",
                                  "Tool approval denied")
            return finish(False, "", "Maximum iterations reached without completion")
        except Exception as error:
            return finish(False, "", redact(f"{type(error).__name__}: {error}"))