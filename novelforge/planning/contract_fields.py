"""Lenient normalisers for the v22 chapter-contract fields.

LLM planners answer in mixed languages and loose shapes.  Every surface that
accepts these fields (Pydantic models, plan parsers, the planner skill) routes
through these helpers so the stored contract only ever contains canonical
values.  Unknown input degrades to ``None`` / an empty list instead of failing
the whole plan window.

- ``polarity``: the direction a chapter *turns* (Story Grid value shift):
  ``"+"`` toward the protagonist's goal, ``"-"`` away from it, ``"±"`` a mixed
  or ironic turn (gain with a cost).
- ``turning_point``: what turns the chapter, ``"action"`` or ``"revelation"``.
- ``promise_actions``: which reader promises (foreshadow rows) the chapter
  plants, reminds, advances or pays off.
- ``witnesses``: non-protagonist characters who must visibly react to a beat
  (usually a payoff beat).
"""
from __future__ import annotations

import re
from typing import Any, Literal, Optional

Polarity = Literal["+", "-", "±"]
TurningPoint = Literal["action", "revelation"]
PromiseActionKind = Literal["plant", "remind", "advance", "payoff"]

POLARITY_VALUES: tuple[str, ...] = ("+", "-", "±")
TURNING_POINT_VALUES: tuple[str, ...] = ("action", "revelation")
PROMISE_ACTION_VALUES: tuple[str, ...] = ("plant", "remind", "advance", "payoff")

BEAT_TYPES: tuple[str, ...] = (
    "setup", "turn", "payoff_beat", "tension_point", "aftermath", "hook",
)

_POLARITY_ALIASES = {
    "+": "+", "positive": "+", "pos": "+", "up": "+", "rise": "+", "gain": "+",
    "正": "+", "正向": "+", "扬": "+", "升": "+", "爽": "+", "好转": "+",
    "-": "-", "−": "-", "–": "-", "negative": "-", "neg": "-", "down": "-",
    "fall": "-", "loss": "-", "负": "-", "负向": "-", "抑": "-", "降": "-",
    "压": "-", "虐": "-", "恶化": "-",
    "±": "±", "+/-": "±", "+-": "±", "-/+": "±", "mixed": "±", "ironic": "±",
    "irony": "±", "bittersweet": "±", "双向": "±", "混合": "±", "得失参半": "±",
    "喜忧参半": "±",
}
_TURNING_ALIASES = {
    "action": "action", "act": "action", "行动": "action", "动作": "action",
    "选择": "action", "抉择": "action",
    "revelation": "revelation", "reveal": "revelation", "揭示": "revelation",
    "揭秘": "revelation", "揭露": "revelation", "发现": "revelation",
    "真相": "revelation",
}
_PROMISE_ACTION_ALIASES = {
    "plant": "plant", "埋": "plant", "埋设": "plant", "埋下": "plant", "埋伏笔": "plant",
    "open": "plant",
    "remind": "remind", "reminder": "remind", "mention": "remind", "提醒": "remind",
    "提及": "remind", "呼应": "remind",
    "advance": "advance", "progress": "advance", "推进": "advance", "加深": "advance",
    "payoff": "payoff", "pay_off": "payoff", "resolve": "payoff", "close": "payoff",
    "兑现": "payoff", "回收": "payoff", "揭晓": "payoff",
}
_SPLIT_RE = re.compile(r"[,，、;；/|]+")


def normalize_polarity(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    key = str(raw).strip().lower()
    if not key:
        return None
    return _POLARITY_ALIASES.get(key)


def normalize_turning_point(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    key = str(raw).strip().lower()
    if not key:
        return None
    return _TURNING_ALIASES.get(key)


def normalize_promise_action_kind(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    return _PROMISE_ACTION_ALIASES.get(str(raw).strip().lower())


def normalize_promise_actions(raw: Any) -> list[dict[str, Any]]:
    """Return ``[{promise_id, label, action, note}]`` dropping unusable items."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        action = normalize_promise_action_kind(item.get("action"))
        promise_id = str(item.get("promise_id") or "").strip()[:100] or None
        label = str(item.get("label") or "").strip()[:200] or None
        if action is None or (promise_id is None and label is None):
            continue
        note = str(item.get("note") or "").strip()[:300] or None
        out.append({
            "promise_id": promise_id, "label": label, "action": action, "note": note,
        })
    return out


def normalize_witnesses(raw: Any, *, limit: int = 8) -> list[str]:
    """Accept a list or a delimited string of character names."""
    if raw is None:
        return []
    if isinstance(raw, str):
        items = _SPLIT_RE.split(raw)
    elif isinstance(raw, (list, tuple)):
        items = [str(v) for v in raw if v is not None]
    else:
        return []
    out: list[str] = []
    for item in items:
        name = str(item).strip()[:60]
        if name and name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out
