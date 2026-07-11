"""Typed contracts used by planning generators and the Agent decision graph."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class MilestoneDraft(BaseModel):
    title: str
    summary: str
    due_start_chapter: Optional[int] = None
    due_end_chapter: Optional[int] = None
    not_before_chapter: Optional[int] = None
    target_state: dict[str, Any] = Field(default_factory=dict)


class StoryArcDraft(BaseModel):
    arc_type: Literal[
        "main", "character", "mystery", "romance", "foreshadow", "world", "other"
    ] = "main"
    title: str
    summary: str
    objective: Optional[str] = None
    start_chapter: int = 1
    end_chapter: Optional[int] = None
    priority: int = Field(default=3, ge=1, le=5)
    constraints: dict[str, Any] = Field(default_factory=dict)
    milestones: list[MilestoneDraft] = Field(default_factory=list)


class StoryPlanPayload(BaseModel):
    title: str
    premise: Optional[str] = None
    ending_direction: Optional[str] = None
    phase_summary: Optional[str] = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    arcs: list[StoryArcDraft] = Field(default_factory=list)


class VolumePlanPayload(BaseModel):
    title: str
    synopsis: str
    objective: Optional[str] = None
    key_results: list[str] = Field(default_factory=list)
    start_chapter: int
    end_chapter: int


class BeatDraft(BaseModel):
    seq: int
    beat_type: Literal["setup", "turn", "payoff_beat", "tension_point", "hook"]
    summary: str
    value_axis: Optional[str] = None
    arc_id: Optional[str] = None
    constraint_level: Literal["soft", "hard"] = "soft"


class ChapterPlanDraft(BaseModel):
    chapter: int
    title: Optional[str] = None
    goal: str
    hook_text: Optional[str] = None
    target_emotion: Optional[str] = None
    opening_hook_type: Optional[str] = None
    hook_type: Optional[str] = None
    expectation_score: Optional[int] = Field(default=None, ge=1, le=5)
    beats: list[BeatDraft] = Field(default_factory=list)


class ChapterRoutePayload(BaseModel):
    route_title: str
    route_summary: str
    chapters: list[ChapterPlanDraft]


class DecisionOptionDraft(BaseModel):
    title: str
    summary: str
    payload: dict[str, Any]
    rationale: Optional[str] = None
    impact: dict[str, Any] = Field(default_factory=dict)
    score: Optional[float] = None


class DecisionRecommendation(BaseModel):
    """Structured result returned by the optional AI option judge."""

    option_index: int = Field(ge=0)
    reason: str


class DecisionEnvelope(BaseModel):
    decision_id: str
    stage: str
    scope_type: str
    scope_id: Optional[str] = None
    question: str
    status: str
    recommended_option_id: Optional[str] = None
    selected_option_id: Optional[str] = None
    context: dict[str, Any] = Field(default_factory=dict)
    options: list[dict[str, Any]] = Field(default_factory=list)
