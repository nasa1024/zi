"""LLMGateway：tier→model 映射 + 重试 + 预算充电（§14.4）。

调用方只感知 ModelTier，不感知具体 model ID 或 SDK 细节。
"""
from __future__ import annotations

import time
from typing import Optional

from .tiers import ModelTier
from .provider import CacheHint, LLMProvider, Message, Response, Tool
from ..budget import BudgetLedger, CircuitBreaker, CircuitTripped


_DEFAULT_MODEL_MAP: dict[str, str] = {
    "fast": "claude-haiku-4-5-20251001",
    "mid": "claude-sonnet-4-6",
    "strong": "claude-opus-4-8",
}

# HTTP 状态码前缀 → 可重试
_RETRYABLE = ("429", "500", "529", "503", "overloaded", "rate_limit")


class LLMGateway:
    def __init__(
        self,
        provider: LLMProvider,
        ledger: BudgetLedger,
        *,
        model_map: Optional[dict[str, str]] = None,
        max_retries: int = 3,
    ) -> None:
        self._provider = provider
        self._ledger = ledger
        self._model_map = {**_DEFAULT_MODEL_MAP, **(model_map or {})}
        self._cb = CircuitBreaker(ledger)
        self._max_retries = max_retries

    def generate(
        self,
        tier: ModelTier,
        messages: list[Message],
        *,
        system: Optional[str] = None,
        tools: Optional[list[Tool]] = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        cache_hint: Optional[CacheHint] = None,
    ) -> Response:
        model = self._model_map[tier.value]
        # 粗估输入 token 数（4 bytes/token）
        est_in = sum(len(str(m.content)) for m in messages) // 4
        self._cb.guard(tokens_in=est_in, tokens_out_est=max_tokens // 2, model=model)

        resp = self._call_with_retry(
            model, messages,
            system=system, tools=tools,
            max_tokens=max_tokens, temperature=temperature,
            cache_hint=cache_hint,
        )
        self._ledger.charge(resp.usage)
        return resp

    def _call_with_retry(
        self, model: str, messages: list[Message], **kw
    ) -> Response:
        last_err: Exception = RuntimeError("no attempts")
        for attempt in range(self._max_retries):
            try:
                return self._provider.generate(messages, model=model, **kw)
            except CircuitTripped:
                raise
            except Exception as e:
                msg = str(e).lower()
                if any(sig in msg for sig in _RETRYABLE):
                    wait = 2 ** attempt
                    time.sleep(wait)
                    last_err = e
                    continue
                raise
        raise last_err

    @property
    def ledger(self) -> BudgetLedger:
        return self._ledger

    def model_for(self, tier: ModelTier) -> str:
        return self._model_map[tier.value]
