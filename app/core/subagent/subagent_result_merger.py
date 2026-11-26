"""子代理结果 → 父会话可用的观察文本。

父 loop 只会看到一段 tool 观察结果，所以这里要把「状态 / 摘要 / 用量 / 步数」压成
一段**有界、可 JSON 解析**的文本：太长会吃掉父会话预算，太散会让模型读不懂。

"有界"是**序列化之后的长度**：JSON 转义会让 `"`/`\\`/控制字符膨胀，所以不能先截
summary 再序列化，必须按最终文本长度迭代收缩（见 `merge_subagent_result`）。

失败也要带足够信息（``failed`` + 原因摘要），但**不要**把子代理的完整对话塞进来——
需要取证时读 ``subagent_runs.result_json``。
"""

import json

from app.common.api.subagent_scheduler import SubagentResult

MAX_OBSERVATION_CHARS = 4000
MIN_OBSERVATION_CHARS = 200
"""低于这个上限时，连 JSON 信封本身都放不下——直接报参数错误，不要悄悄超限。"""

_TRUNCATION_MARK = "…[truncated]"


def _observation(
    status: str, summary: str, tokens: int, steps: int
) -> str:
    return json.dumps(
        {
            "subagent_status": status,
            "summary": summary,
            "tokens": tokens,
            "steps": steps,
        },
        ensure_ascii=False,
    )


def merge_subagent_result(
    result: SubagentResult, *, max_chars: int = MAX_OBSERVATION_CHARS
) -> str:
    if max_chars < MIN_OBSERVATION_CHARS:
        raise ValueError(
            f"max_chars must be >= {MIN_OBSERVATION_CHARS} (got {max_chars})"
        )
    summary = result.summary or ""
    text = _observation(result.status, summary, result.tokens, result.steps)
    if len(text) <= max_chars:
        return text

    # 二分收缩 summary，保证**序列化后**（含转义膨胀）不超过上限。
    low, high = 0, len(summary)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = _observation(
            result.status, summary[:middle] + _TRUNCATION_MARK, result.tokens, result.steps
        )
        if len(candidate) <= max_chars:
            low = middle
        else:
            high = middle - 1
    truncated = summary[:low] + _TRUNCATION_MARK
    text = _observation(result.status, truncated, result.tokens, result.steps)
    if len(text) > max_chars:  # 极端情况下 summary 为空仍超限（status 很长等）
        raise ValueError(
            f"cannot fit subagent observation into {max_chars} chars; "
            f"lower bound is {len(text)}"
        )
    return text


def result_json(result: SubagentResult) -> str:
    """落 ``subagent_runs.result_json`` 的完整结果（含 detail，形状统一）。"""
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
