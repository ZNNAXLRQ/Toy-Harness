"""可编程的 Provider 替身：按脚本返回响应，并记录每次请求。"""

from copy import deepcopy

from app.common.api.llm_provider import CompletionRequest, ModelResponse


class ScriptedProvider:
    provider_name = "scripted"

    def __init__(self, responses: list[ModelResponse | Exception]) -> None:
        self._responses = list(responses)
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._responses:
            raise RuntimeError("ScriptedProvider has no more scripted responses")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)
