"""Typed contracts used by planning generators and the Agent decision graph."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from ..craft.hooks import ENDING_HOOK_TYPES, OPENING_HOOK_TYPES

OpeningHookType = Literal[*tuple(OPENING_HOOK_TYPES), "other"]
EndingHookType = Literal[*tuple(ENDING_HOOK_TYPES), "other"]
PlanAction = Literal["keep", "patch", "replan", "pause"]
DivergenceLevel = Literal["normal", "minor", "major", "severe"]


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
    arc_targets: list[str] = Field(default_factory=list)
    volume_kr_targets: list[str] = Field(default_factory=list)
    milestone_targets: list[str] = Field(default_factory=list)
    must_advance: list[str] = Field(default_factory=list)
    must_resolve: list[str] = Field(default_factory=list)
    must_include_entities: list[str] = Field(default_factory=list)
    must_not_reveal: list[str] = Field(default_factory=list)
    must_not_resolve: list[str] = Field(default_factory=list)
    forbidden_state_changes: list[str] = Field(default_factory=list)
    target_state: dict[str, Any] = Field(default_factory=dict)
    success_criteria: list[str] = Field(default_factory=list)
    hook_text: Optional[str] = None
    target_emotion: Optional[str] = None
    opening_hook_type: Optional[OpeningHookType] = None
    hook_type: Optional[EndingHookType] = None
    expectation_score: Optional[int] = Field(default=None, ge=1, le=5)
    beats: list[BeatDraft] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_target_state(self):
        if self.target_state and not self.success_criteria:
            raise ValueError("target_state requires at least one success criterion")
        return self


class ChapterRoutePayload(BaseModel):
    route_title: str
    route_summary: str
    chapters: list[ChapterPlanDraft]

    @model_validator(mode="after")
    def validate_route(self):
        if not self.chapters:
            raise ValueError("route must contain chapters")

        numbers = [item.chapter for item in self.chapters]
        if len(numbers) != len(set(numbers)):
            raise ValueError("duplicate chapters")
        if numbers != list(range(numbers[0], numbers[0] + len(numbers))):
            raise ValueError("chapters must be consecutive and ordered")

        for chapter in self.chapters:
            if not chapter.beats:
                raise ValueError(f"chapter {chapter.chapter} must contain beats")
            if chapter.beats[-1].beat_type != "hook":
                raise ValueError(f"chapter {chapter.chapter} last beat must be hook")
            if not any(beat.constraint_level == "hard" for beat in chapter.beats):
                raise ValueError(f"chapter {chapter.chapter} must contain a hard beat")
        return self


class EvidenceSpan(BaseModel):
    """Character offsets and the supporting verbatim excerpt in the final draft."""

    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    text: str
    label: Optional[str] = None

    @model_validator(mode="after")
    def validate_offsets(self):
        if self.end_char < self.start_char:
            raise ValueError("end_char must be greater than or equal to start_char")
        return self


class ActualEvent(BaseModel):
    summary: str
    event_type: str = "other"
    involved_entities: list[str] = Field(default_factory=list)
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class StateChange(BaseModel):
    subject: str
    field: str
    before: Any = None
    after: Any = None
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class BeatEvidence(BaseModel):
    beat_seq: int = Field(ge=1)
    completed: bool
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ArcProgress(BaseModel):
    arc_id: str
    summary: str
    progress_score: float = Field(ge=0.0, le=1.0)
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class RevealEvidence(BaseModel):
    information: str
    forbidden: bool = False
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ChapterActuals(BaseModel):
    chapter: int = Field(ge=1)
    draft_id: Optional[str] = None
    events: list[ActualEvent] = Field(default_factory=list)
    state_changes: list[StateChange] = Field(default_factory=list)
    completed_beats: list[BeatEvidence] = Field(default_factory=list)
    arc_progress: list[ArcProgress] = Field(default_factory=list)
    revealed_information: list[RevealEvidence] = Field(default_factory=list)
    unresolved_promises: list[str] = Field(default_factory=list)
    involved_entities: list[str] = Field(default_factory=list)
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    extractor_version: Optional[str] = None


class AlignmentFinding(BaseModel):
    category: Literal["goal", "beat", "arc", "volume", "reveal"]
    target: str
    matched: bool
    score: float = Field(ge=0.0, le=1.0)
    explanation: str = ""
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class PlanAlignmentReport(BaseModel):
    chapter: int = Field(ge=1)
    plan_version_id: Optional[str] = None
    actuals_id: Optional[str] = None
    goal_score: float = Field(ge=0.0, le=1.0)
    beat_score: float = Field(ge=0.0, le=1.0)
    arc_score: float = Field(ge=0.0, le=1.0)
    volume_score: float = Field(ge=0.0, le=1.0)
    reveal_score: float = Field(ge=0.0, le=1.0)
    divergence_score: float = Field(ge=0.0, le=1.0)
    divergence_level: DivergenceLevel
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    decision: Optional[PlanAction] = None
    findings: list[AlignmentFinding] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)
    evaluator_version: Optional[str] = None


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
