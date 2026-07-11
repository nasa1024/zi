"""Future story planning and choice-driven authoring support.

Planning data is intentionally separate from canon.  `memory.recall` remains an
as-of view of what has happened; this package supplies author-only future intent.
"""

from .context import PlanContext, gather_plan_context
from .generation import recommend_decision_option
from .repository import (
    apply_chapter_route,
    apply_story_plan,
    apply_volume_plan,
    create_decision,
    load_decision,
    resolve_decision,
    story_map,
)

__all__ = [
    "PlanContext",
    "gather_plan_context",
    "recommend_decision_option",
    "apply_chapter_route",
    "apply_story_plan",
    "apply_volume_plan",
    "create_decision",
    "load_decision",
    "resolve_decision",
    "story_map",
]
