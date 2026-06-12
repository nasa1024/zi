"""FakeProvider：测试/离线用存根，无网络调用。

通过 responses 列表或 factory 回调注入返回值。
"""
from __future__ import annotations

from .provider import CapabilitySet, CacheHint, Message, Response, Tool, Usage


class FakeProvider:
    """可注入固定响应的假 provider，供单测使用。"""

    def __init__(
        self,
        responses: list[str] | None = None,
        factory=None,
    ) -> None:
        self._responses = list(responses or [])
        self._factory = factory
        self._calls: list[dict] = []

    def generate(
        self,
        messages: list[Message],
        *,
        model: str,
        max_tokens: int = 4096,
        system: str | None = None,
        tools: list[Tool] | None = None,
        temperature: float = 1.0,
        cache_hint: CacheHint | None = None,
    ) -> Response:
        self._calls.append(dict(messages=messages, model=model, system=system,
                                cache_hint=cache_hint))
        if self._factory:
            text = self._factory(messages, model=model)
        elif self._responses:
            text = self._responses.pop(0)
        else:
            text = f"[fake:{model}] " + (
                messages[-1].content if messages else ""
            )
        usage = Usage(input=100, output=len(text) // 4 + 1, provider="fake", model=model)
        return Response(text=text, usage=usage)

    def capabilities(self, model: str) -> CapabilitySet:
        return CapabilitySet(supports_tools=True, supports_cache=False, max_tokens_out=8192)

    @property
    def calls(self) -> list[dict]:
        return self._calls
