"""子代理结果 → 父会话可用的观察文本。

父 loop 只会看到一段 tool 观察结果，所以这里要把「状态 / 摘要 / 用量 / 步数」压成
一段**有界、可 JSON 解析**的文本：太长会吃掉父会话预算，太散会让模型读不懂。

失败也要带足够信息（``failed`` + 原因摘要），但**不要**把子代理的完整对话塞进来——
需要取证时读 ``subagent_runs.result_json``。
"""

import json

from app.common.api.subagent_scheduler import SubagentResult

MAX_OBSERVATION_CHARS = 4000


def merge_subagent_result(
    result: SubagentResult, *, max_chars: int = MAX_OBSERVATION_CHARS
) -> str:
    summary = result.summary or ""
    text = json.dumps(
        {
            "subagent_status": result.status,
            "summary": summary,
            "tokens": result.tokens,
            "steps": result.steps,
        },
        ensure_ascii=False,
    )
    if len(text) <= max_chars:
        return text
    keep = max_chars - 200
    truncated = summary[: max(0, keep)]
    return json.dumps(
        {
            "subagent_status": result.status,
            "summary": truncated + "…[truncated]",
            "tokens": result.tokens,
            "steps": result.steps,
        },
        ensure_ascii=False,
    )


def result_json(result: SubagentResult) -> str:
    """落 ``subagent_runs.result_json`` 的完整结果（含 detail）。"""
    return json.dumps(
        {
            "status": result.status,
            "summary": result.summary,
            "tokens": result.tokens,
            "steps": result.steps,
            "detail": dict(result.detail or {}),
        },
        ensure_ascii=False,
    )
