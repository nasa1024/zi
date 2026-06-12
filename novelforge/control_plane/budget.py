"""BudgetLedger + CircuitBreaker（§14.6 / §12）。

每章一个 Ledger 实例；CircuitBreaker 在 generate 前 guard() 检查剩余预算。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field


# 粗略定价（$/1M tokens），按各平台公开价（cache_hit 价另行忽略）
_PRICING: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-haiku-4-5-20251001": (0.8, 4.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8": (15.0, 75.0),
    # DeepSeek V3（旧）
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    # DeepSeek V4
    "deepseek-v4-flash": (0.27, 1.10),   # V4 Flash（参考 V3 价格，官方未公布时用此估算）
    "deepseek-v4-pro": (0.55, 2.19),     # V4 Pro（参考 R1 价格，官方未公布时用此估算）
}


def _estimate_cost(tokens_in: int, tokens_out: int, model: str) -> float:
    in_p, out_p = _PRICING.get(model, (3.0, 15.0))
    return (tokens_in * in_p + tokens_out * out_p) / 1_000_000


class CircuitTripped(Exception):
    def __init__(self, reason: str, spent: float, cap: float) -> None:
        super().__init__(
            f"circuit_tripped:{reason} spent={spent:.4f} cap={cap:.4f}"
        )
        self.reason = reason
        self.spent = spent
        self.cap = cap


@dataclass
class BudgetLedger:
    max_tokens: int = 200_000
    max_usd: float = 2.0
    max_revise_rounds: int = 3
    _tokens_spent: int = field(default=0, init=False, repr=False)
    _usd_spent: float = field(default=0.0, init=False, repr=False)
    _revise_rounds: int = field(default=0, init=False, repr=False)
    _cache_read_tokens: int = field(default=0, init=False, repr=False)
    # 多候选并行生成时各线程共享同一账本，累加必须原子
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def charge(self, usage) -> None:
        """接受 provider.Usage 或含 input/output 属性的对象。线程安全。"""
        ti = getattr(usage, "input", 0) or 0
        to = getattr(usage, "output", 0) or 0
        model = getattr(usage, "model", "") or ""
        cost = _estimate_cost(ti, to, model)
        # M1-⑥ 观测：前缀缓存命中量（DeepSeek prompt_cache_hit / Anthropic cache_read）
        cache_read = getattr(usage, "cache_read", 0) or 0
        with self._lock:
            self._tokens_spent += ti + to
            self._usd_spent += cost
            self._cache_read_tokens += cache_read

    def charge_revise_round(self) -> None:
        with self._lock:
            self._revise_rounds += 1

    @property
    def cache_read_tokens(self) -> int:
        return self._cache_read_tokens

    @property
    def tokens_spent(self) -> int:
        return self._tokens_spent

    @property
    def usd_spent(self) -> float:
        return self._usd_spent

    @property
    def revise_rounds(self) -> int:
        return self._revise_rounds

    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self._tokens_spent)


@dataclass
class CircuitBreaker:
    ledger: BudgetLedger

    def guard(self, tokens_in: int, tokens_out_est: int, model: str) -> None:
        """调用 generate 前检查预算；超限抛 CircuitTripped。"""
        future_tok = self.ledger.tokens_spent + tokens_in + tokens_out_est
        if future_tok > self.ledger.max_tokens:
            raise CircuitTripped("tokens", float(future_tok), float(self.ledger.max_tokens))
        future_usd = self.ledger.usd_spent + _estimate_cost(tokens_in, tokens_out_est, model)
        if future_usd > self.ledger.max_usd:
            raise CircuitTripped("usd", future_usd, self.ledger.max_usd)
        if self.ledger.revise_rounds >= self.ledger.max_revise_rounds:
            raise CircuitTripped(
                "revise_rounds",
                float(self.ledger.revise_rounds),
                float(self.ledger.max_revise_rounds),
            )
