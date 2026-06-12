"""FakeProvider：测试/离线用存根，无网络调用。

通过 responses 列表或 factory 回调注入返回值。
factory 若声明 temperature 参数会收到调用温度——多候选并行生成时调用顺序
不确定，测试用温度（候选 i 固定为 1.0-i*spread）做确定性映射。
"""
from __future__ import annotations

import inspect
import threading

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
        self._factory_wants_temperature = False
        if factory is not None:
            params = inspect.signature(factory).parameters
            self._factory_wants_temperature = (
                "temperature" in params
                or any(p.kind == p.VAR_KEYWORD for p in params.values())
            )
        self._calls: list[dict] = []
        self._lock = threading.Lock()  # 并行候选生成时多线程并发调用

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
        with self._lock:
            self._calls.append(dict(messages=messages, model=model, system=system,
                                    temperature=temperature, cache_hint=cache_hint))
        if self._factory:
            if self._factory_wants_temperature:
                text = self._factory(messages, model=model, temperature=temperature)
            else:
                text = self._factory(messages, model=model)
        elif self._responses:
            with self._lock:
                text = self._responses.pop(0) if self._responses else ""
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
