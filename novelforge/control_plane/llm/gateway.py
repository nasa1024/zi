"""LLMGateway：tier→model 映射 + 重试 + 预算充电（§14.4）。

调用方只感知 ModelTier，不感知具体 model ID 或 SDK 细节。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from .tiers import ModelTier
from .provider import CacheHint, LLMProvider, Message, Response, Tool
from ..budget import BudgetLedger, CircuitBreaker, CircuitTripped


_TIER_ORDER = (ModelTier.FAST, ModelTier.MID, ModelTier.STRONG)


@dataclass
class ValidatedResult:
    """generate_validated 产物：parse 成功值 + 实际档位 + 是否升级过。"""
    value: object | None      # parse 成功的产物；全档失败为 None
    response: Response        # 最后一次调用的原始响应（成本已计入 ledger）
    tier_used: str            # 实际产出档位
    escalated: bool           # 是否发生过升级


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

    def generate_validated(
        self,
        tier: ModelTier,
        messages: list[Message],
        *,
        parse: Callable[[str], object],
        system: Optional[str] = None,
        tools: Optional[list[Tool]] = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        cache_hint: Optional[CacheHint] = None,
        max_tier: ModelTier = ModelTier.STRONG,
    ) -> ValidatedResult:
        """P2#14：弱模型分层 + 失败升级。

        从 tier 起逐档调用，每档跑 parse(text)；返回非 None 即成功，
        否则升一档（fast→mid→strong）重试，封顶 max_tier。parse 抛异常视同 None。
        全档失败返回 value=None。每次调用都计入 ledger（升级即多花，成本看板可见）。
        max_tier=tier 时退化为单档（等价旧行为，关闭升级）。
        """
        start = _TIER_ORDER.index(tier)
        end = _TIER_ORDER.index(max_tier)
        last_resp: Optional[Response] = None
        used = tier
        for t in _TIER_ORDER[start:end + 1]:
            used = t
            resp = self.generate(
                t, messages, system=system, tools=tools,
                max_tokens=max_tokens, temperature=temperature, cache_hint=cache_hint,
            )
            last_resp = resp
            try:
                value = parse(resp.text)
            except Exception:
                value = None
            if value is not None:
                return ValidatedResult(
                    value=value, response=resp, tier_used=t.value,
                    escalated=(t != tier))
        return ValidatedResult(
            value=None, response=last_resp, tier_used=used.value,
            escalated=(used != tier))

    @property
    def ledger(self) -> BudgetLedger:
        return self._ledger

    def model_for(self, tier: ModelTier) -> str:
        return self._model_map[tier.value]
