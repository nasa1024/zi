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

    requested = os.environ.get("NOVELFORGE_PROVIDER", "").strip().lower()
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if requested == "fake":
        cfg.provider.provider = "fake"
        cfg.provider.api_key = None
    elif requested in {"deepseek", "deepseek_anthropic"}:
        cfg.provider.provider = requested
        cfg.provider.api_key = deepseek_key
    elif requested == "anthropic":
        cfg.provider.provider = "anthropic"
        cfg.provider.api_key = anthropic_key
    elif requested in {"openai", "openai_compat"}:
        cfg.provider.provider = "openai_compat"
        cfg.provider.api_key = openai_key
    elif requested:
        # Keep an explicit unsupported provider visible to the factory instead
        # of silently pairing it with whichever unrelated key happens to exist.
        cfg.provider.provider = requested
        cfg.provider.api_key = None
    elif deepseek_key:
        cfg.provider.provider = "deepseek"
        cfg.provider.api_key = deepseek_key
    elif anthropic_key:
        cfg.provider.provider = "anthropic"
        cfg.provider.api_key = anthropic_key
    elif openai_key:
        cfg.provider.provider = "openai_compat"
        cfg.provider.api_key = openai_key

    if cfg.provider.provider == "openai_compat" and cfg.provider.api_key:
        cfg.provider.base_url = (
            os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("NOVELFORGE_OPENAI_BASE_URL")
            or cfg.provider.base_url
        )
        cfg.provider.fast_model = os.environ.get(
            "NOVELFORGE_FAST_MODEL", cfg.provider.fast_model
        )
        cfg.provider.mid_model = os.environ.get(
            "NOVELFORGE_MID_MODEL", cfg.provider.mid_model
        )
        cfg.provider.strong_model = os.environ.get(
            "NOVELFORGE_STRONG_MODEL", cfg.provider.strong_model
        )
    return cfg


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
