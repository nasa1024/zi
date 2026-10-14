"""Single runtime entry point for project-scoped LLM gateways.

Creative planning, chapter generation, memory settlement, and API helpers used
to assemble providers independently.  Keeping that logic here prevents two
entry points from silently using different credentials or model families.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

from ...config import (
    NovelForgeConfig,
    apply_project_memory_settings,
    apply_runtime_env,
)
from ..budget import BudgetLedger
from . import factory as llm_factory
from .gateway import LLMGateway


def build_configured_gateway(
    cfg: NovelForgeConfig,
    ledger: Optional[BudgetLedger] = None,
) -> LLMGateway:
    """Construct a gateway from an already-resolved config.

    Request-specific callers may adjust budgets, governance, or quality after
    :func:`build_project_config`; construction still flows through this module
    instead of importing the low-level provider factory throughout the app.
    """
    return llm_factory.build_gateway(cfg, ledger)


def resolve_provider_env() -> tuple[Optional[str], Optional[str]]:
    """Resolve ``(provider, api_key)`` from the process env without cross-wiring.

    An explicit ``NOVELFORGE_PROVIDER`` is only ever paired with its own key.
    Without one, the provider is inferred from the available keys; Gemini wins
    because it is the default creative model family.  ``GOOGLE_API_KEY`` is
    too generic to drive inference and is only honoured for explicit Gemini.
    Returns ``(None, None)`` when nothing is configured.
    """
    requested = os.environ.get("NOVELFORGE_PROVIDER", "").strip().lower()
    gemini_key = os.environ.get("GEMINI_API_KEY")
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if requested == "fake":
        return "fake", None
    if requested in {"gemini", "google"}:
        return "gemini", gemini_key or os.environ.get("GOOGLE_API_KEY")
    if requested in {"deepseek", "deepseek_anthropic"}:
        return requested, deepseek_key
    if requested == "anthropic":
        return "anthropic", anthropic_key
    if requested in {"openai", "openai_compat"}:
        return "openai_compat", openai_key
    if requested:
        # Keep an explicit unsupported provider visible to the factory instead
        # of silently pairing it with whichever unrelated key happens to exist.
        return requested, None
    if gemini_key:
        return "gemini", gemini_key
    if deepseek_key:
        return "deepseek", deepseek_key
    if anthropic_key:
        return "anthropic", anthropic_key
    if openai_key:
        return "openai_compat", openai_key
    return None, None


def _apply_tier_model_env(cfg: NovelForgeConfig) -> None:
    cfg.provider.fast_model = os.environ.get(
        "NOVELFORGE_FAST_MODEL", cfg.provider.fast_model
    )
    cfg.provider.mid_model = os.environ.get(
        "NOVELFORGE_MID_MODEL", cfg.provider.mid_model
    )
    cfg.provider.strong_model = os.environ.get(
        "NOVELFORGE_STRONG_MODEL", cfg.provider.strong_model
    )


def apply_provider_env(
    cfg: NovelForgeConfig, *, api_key: Optional[str] = None,
) -> None:
    """Inject provider, credential, endpoint and tier models from the env.

    ``api_key`` is a legacy explicit credential: it replaces the env key of the
    resolved provider (``deepseek`` when the env names none).
    """
    provider, key = resolve_provider_env()
    if api_key:
        provider = provider or "deepseek"
        if provider != "fake":
            key = api_key
    if provider is None:
        return
    cfg.provider.provider = provider
    cfg.provider.api_key = key

    if provider == "openai_compat" and key:
        cfg.provider.base_url = (
            os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("NOVELFORGE_OPENAI_BASE_URL")
            or cfg.provider.base_url
        )
        _apply_tier_model_env(cfg)
    elif provider == "gemini":
        cfg.provider.base_url = (
            os.environ.get("GEMINI_BASE_URL")
            or os.environ.get("NOVELFORGE_GEMINI_BASE_URL")
            or cfg.provider.base_url
        )
        cfg.provider.safety_threshold = (
            os.environ.get("NOVELFORGE_GEMINI_SAFETY_THRESHOLD")
            or cfg.provider.safety_threshold
        )
        # Unset tiers keep the Gemini 4 defaults chosen by the factory.
        _apply_tier_model_env(cfg)


def build_project_config(
    project_id: str,
    db_path: str,
    conn: sqlite3.Connection | None = None,
) -> NovelForgeConfig:
    """Build the effective project config without constructing a provider."""
    cfg = NovelForgeConfig(project_id=project_id, db_path=db_path)
    if conn is not None:
        apply_project_memory_settings(cfg, conn)
    apply_runtime_env(cfg)
    apply_provider_env(cfg)
    return cfg


DEFAULT_PLANNING_HORIZON = 10        # 普通模型：单次规划 ≤10 章防输出截断
LONG_OUTPUT_PLANNING_HORIZON = 30    # 长输出模型（Gemini 4 long decode）：单次 ≤30 章


def max_planning_horizon(cfg: NovelForgeConfig) -> int:
    """Chapters one volume-planning call may cover for the configured model.

    The legacy 10-chapter window exists only to avoid truncated plan JSON.
    Models that report ``long_output`` (Gemini 4 continues past the per-request
    cap server-side) can plan a much larger slice of a volume in one pass.
    """
    try:
        provider = llm_factory.build_provider(cfg.provider)
        strong = llm_factory._model_map(cfg.provider)["strong"]
        caps = provider.capabilities(strong)
    except Exception:
        return DEFAULT_PLANNING_HORIZON
    if getattr(caps, "long_output", False):
        return LONG_OUTPUT_PLANNING_HORIZON
    return DEFAULT_PLANNING_HORIZON


def build_project_gateway(
    project_id: str,
    db_path: str,
    conn: sqlite3.Connection | None = None,
    *,
    ledger: Optional[BudgetLedger] = None,
    allow_missing: bool = False,
) -> LLMGateway | None:
    """Return the one effective gateway for a project.

    ``None`` is returned when no creative provider is configured.  Callers that
    intentionally support an offline deterministic fallback should pass
    ``allow_missing=True``; the flag documents that behavior at the call site.
    """
    cfg = build_project_config(project_id, db_path, conn)
    if cfg.provider.provider != "fake" and not cfg.provider.api_key:
        if not allow_missing:
            return None
        # Keep offline/deterministic callers and test-injected factories
        # working.  A real credential-less provider may fail to construct, in
        # which case the caller receives the documented None fallback.
        try:
            return build_configured_gateway(cfg, ledger)
        except Exception:
            return None
    return build_configured_gateway(cfg, ledger)
