"""v22 feature-extension registry for the chapter pipeline.

Several reader-experience features plug into the same four seams.  Each feature
lives in its own module under ``novelforge.craft`` and may define any of:

``craft_checks(ctx) -> list[dict]``
    Deterministic, read-only checks run inside ``CraftCheckSkill`` (which runs
    several times per chapter: initial check, every revise round, after polish
    and in manual candidate validation).  Return issue dicts with keys
    ``check, severity ('warn'|'block'), detail`` and optionally ``span, fix,
    repair_scope ('local'|'structural'), category``.  Must tolerate missing
    workspace keys (the governance API runs the skill with only draft/beats)
    and old databases.  Prefer ``warn``: a remaining ``block`` counts toward
    autopilot degradation.

``draft_brief(conn, chapter, workspace, *, language, cfg) -> str``
    A short dynamic prompt block computed before planning/drafting a chapter
    (never part of the cached stable prefix).  Rendered for the planner and the
    drafter after the chapter contract.

``enrich_actuals(actuals, draft_text, plan_context) -> None``
    Deterministically mutate ``ChapterActuals`` (no LLM, no DB) right after the
    actuals extractor runs; evidence offsets are relative to ``draft_text``.

``settle(conn, chapter, workspace, *, gateway, cfg) -> dict``
    A post-commit step run inside ``Orchestrator._settle_chapter`` after the
    foreshadow settlement.  May write the database (the orchestrator commits
    afterwards) and returns a JSON-serialisable report stored in
    ``pipeline_run.detail_json["extensions"][<module>]``.  Raising marks only
    this step degraded, so catch expected failures and report them instead.

A failing extension never breaks the chapter: every call is isolated.
"""
from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Order matters only for prompt-brief and report ordering.
EXTENSION_MODULES: tuple[str, ...] = (
    "promise_ledger",     # F1 读者承诺账本
    "deus_ex_machina",    # F2 "天降外挂"拦截
    "polarity",           # F3 极性序列与抑扬上限
    "payoff_reactions",   # F5 爽点三段式
)

_ISSUE_SEVERITIES = {"warn", "block"}
_REPAIR_SCOPES = {"local", "structural"}


def _load(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(f"{__package__}.{name}")
    except ImportError:
        logger.exception("craft extension %s failed to import", name)
        return None


def _hooks(attr: str) -> list[tuple[str, Callable[..., Any]]]:
    out: list[tuple[str, Callable[..., Any]]] = []
    for name in EXTENSION_MODULES:
        module = _load(name)
        fn = getattr(module, attr, None) if module is not None else None
        if callable(fn):
            out.append((name, fn))
    return out


def normalize_issue(raw: Any, *, module: str) -> dict | None:
    """Coerce one extension issue into the craft issue dict shape."""
    if not isinstance(raw, dict):
        return None
    detail = str(raw.get("detail") or raw.get("issue") or "").strip()
    check = str(raw.get("check") or module).strip()
    if not detail or not check:
        return None
    severity = str(raw.get("severity") or "warn")
    scope = str(raw.get("repair_scope") or "local")
    return {
        "check": check,
        "severity": severity if severity in _ISSUE_SEVERITIES else "warn",
        "detail": detail,
        "span": str(raw.get("span") or raw.get("evidence") or ""),
        "fix": str(raw.get("fix") or ""),
        "repair_scope": scope if scope in _REPAIR_SCOPES else "local",
        "category": str(raw.get("category") or f"craft.{check}"),
    }


def run_craft_checks(ctx: Any) -> list[dict]:
    issues: list[dict] = []
    for name, fn in _hooks("craft_checks"):
        try:
            for raw in fn(ctx) or []:
                issue = normalize_issue(raw, module=name)
                if issue is not None:
                    issues.append(issue)
        except Exception:
            logger.exception("craft extension %s.craft_checks failed", name)
    return issues


def build_draft_briefs(
    conn: Any, chapter: int, workspace: dict, *, language: str, cfg: Any,
) -> str:
    blocks: list[str] = []
    for name, fn in _hooks("draft_brief"):
        try:
            block = fn(conn, chapter, workspace, language=language, cfg=cfg)
        except Exception:
            logger.exception("craft extension %s.draft_brief failed", name)
            continue
        if isinstance(block, str) and block.strip():
            blocks.append(block.strip())
    return "\n\n".join(blocks)


def enrich_actuals(actuals: Any, draft_text: str, plan_context: Any) -> None:
    for name, fn in _hooks("enrich_actuals"):
        try:
            fn(actuals, draft_text, plan_context)
        except Exception:
            logger.exception("craft extension %s.enrich_actuals failed", name)


def settle_steps() -> list[tuple[str, Callable[..., Any]]]:
    """``[(module_name, settle_fn)]`` for the orchestrator's degradable steps."""
    return _hooks("settle")
