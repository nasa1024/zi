"""Extract and persist the observable outcome of a finished chapter draft.

The extractor is deliberately deterministic.  It records evidence spans from
the *final* text, while proposal extraction remains a compatible optional
channel.  A chapter with no world-state changes therefore has a valid empty
proposal list instead of being treated as a failed draft.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..ids import new_id
from .context import PlanContext
from .models import (
    ActualEvent,
    ArcProgress,
    BeatEvidence,
    ChapterActuals,
    EvidenceSpan,
    RevealEvidence,
    StateChange,
)


EXTRACTOR_VERSION = "deterministic-v1"
_SENTENCE_RE = re.compile(r"[^\n\r。！？!?]+(?:[。！？!?]|$)")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]+")


@dataclass(frozen=True)
class ActualsExtraction:
    """In-memory extraction result used by the settlement pipeline."""

    actuals: ChapterActuals
    proposals: list[dict[str, Any]]
    extractor_version: str = EXTRACTOR_VERSION


def _context_dict(context: PlanContext | dict[str, Any] | None) -> dict[str, Any]:
    if context is None:
        return {}
    if isinstance(context, PlanContext):
        return context.to_dict()
    return context


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    result: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = item.get("text") or item.get("title") or item.get("summary") or item.get("id")
        else:
            text = item
        text = str(text or "").strip()
        if text:
            result.append(text)
    return result


def _terms(text: str) -> set[str]:
    terms: set[str] = set()
    for token in _WORD_RE.findall((text or "").lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            if len(token) <= 3:
                terms.add(token)
            terms.update(token[i:i + 2] for i in range(max(0, len(token) - 1)))
        else:
            terms.add(token)
    return {term for term in terms if term}


def text_match_score(target: str, evidence_text: str) -> float:
    """Return target-term coverage in ``evidence_text`` on a 0..1 scale."""
    target = str(target or "").strip()
    if not target:
        return 1.0
    if target in evidence_text:
        return 1.0
    wanted = _terms(target)
    if not wanted:
        return 0.0
    present = _terms(evidence_text)
    return min(1.0, len(wanted & present) / len(wanted))


def _sentences(text: str) -> list[tuple[int, int, str]]:
    rows: list[tuple[int, int, str]] = []
    for match in _SENTENCE_RE.finditer(text or ""):
        raw = match.group(0)
        stripped = raw.strip()
        if not stripped:
            continue
        left = len(raw) - len(raw.lstrip())
        start = match.start() + left
        rows.append((start, start + len(stripped), stripped))
    return rows


def find_evidence_span(
    text: str,
    target: str,
    *,
    label: str | None = None,
    threshold: float = 0.34,
) -> EvidenceSpan | None:
    """Find the strongest sentence-level evidence for a plan target."""
    target = str(target or "").strip()
    if not target or not text:
        return None
    exact = text.find(target)
    if exact >= 0:
        return EvidenceSpan(
            start_char=exact,
            end_char=exact + len(target),
            text=text[exact:exact + len(target)],
            label=label,
        )
    best: tuple[float, int, int, str] | None = None
    for start, end, sentence in _sentences(text):
        score = text_match_score(target, sentence)
        if best is None or score > best[0]:
            best = (score, start, end, sentence)
    if best is None or best[0] < threshold:
        return None
    return EvidenceSpan(
        start_char=best[1],
        end_char=best[2],
        text=best[3],
        label=label,
    )


def _dedupe_spans(spans: Iterable[EvidenceSpan]) -> list[EvidenceSpan]:
    seen: set[tuple[int, int, str | None]] = set()
    result: list[EvidenceSpan] = []
    for span in spans:
        key = (span.start_char, span.end_char, span.label)
        if key not in seen:
            seen.add(key)
            result.append(span)
    return result


def _proposal_state_change(
    proposal: dict[str, Any], draft_text: str,
) -> StateChange | None:
    new = proposal.get("new") if isinstance(proposal.get("new"), dict) else {}
    subject = str(new.get("subject") or proposal.get("entity") or "").strip()
    field = str(new.get("predicate") or new.get("facet") or proposal.get("fact_type") or "state").strip()
    after = new.get("object", new.get("value", new.get("after")))
    before = new.get("before", proposal.get("old"))
    if not subject and after is None:
        return None
    evidence_target = str(after if after is not None else subject)
    evidence = find_evidence_span(
        draft_text, evidence_target, label="state_change", threshold=0.25
    )
    return StateChange(
        subject=subject or "unknown",
        field=field or "state",
        before=before,
        after=after,
        evidence_spans=[evidence] if evidence else [],
        confidence=0.9 if evidence else 0.55,
    )


def _proposal_evidence(
    proposal: dict[str, Any], draft_text: str, target: str,
) -> EvidenceSpan | None:
    for ref in proposal.get("evidence_refs") or []:
        if not isinstance(ref, dict):
            continue
        quote = str(ref.get("quote") or "").strip()
        start = draft_text.find(quote)
        if quote and start >= 0:
            return EvidenceSpan(
                start_char=start,
                end_char=start + len(quote),
                text=quote,
                label="proposal_evidence",
            )
    return find_evidence_span(
        draft_text, target, label="proposal_evidence", threshold=0.25
    )


def _milestone_keywords(item: dict[str, Any]) -> list[str]:
    target_state = item.get("target_state") or {}
    return _strings(target_state.get("keywords"))


def extract_chapter_actuals(
    chapter: int,
    draft_text: str,
    plan_context: PlanContext | dict[str, Any] | None,
    *,
    draft_id: str | None = None,
    seed_proposals: Sequence[dict[str, Any]] | None = None,
) -> ActualsExtraction:
    """Extract structured actuals and evidence from the final chapter text.

    ``seed_proposals`` is the draft model's optional state-change channel.  It
    is normalized and retained when supplied, but absence is fully legal.
    """
    context = _context_dict(plan_context)
    contract = context.get("current_contract") or {}
    proposals = [dict(item) for item in (seed_proposals or []) if isinstance(item, dict)]

    targets: list[tuple[str, str]] = []
    goal = str(contract.get("goal") or contract.get("summary") or "").strip()
    if goal:
        targets.append(("goal", goal))
    for field in ("must_advance", "must_resolve", "success_criteria"):
        targets.extend((field, item) for item in _strings(contract.get(field)))

    events: list[ActualEvent] = []
    evidence_spans: list[EvidenceSpan] = []
    involved_entities = [
        entity for entity in _strings(contract.get("must_include_entities"))
        if entity in draft_text
    ]
    for label, target in targets:
        evidence = find_evidence_span(draft_text, target, label=label)
        if evidence is None:
            continue
        evidence_spans.append(evidence)
        events.append(ActualEvent(
            summary=target,
            event_type="goal_progress" if label == "goal" else label,
            involved_entities=[entity for entity in involved_entities if entity in evidence.text],
            evidence_spans=[evidence],
            confidence=0.95 if evidence.text == target else 0.72,
        ))

    for proposal in proposals:
        new = proposal.get("new") if isinstance(proposal.get("new"), dict) else {}
        subject = str(new.get("subject") or proposal.get("entity") or "").strip()
        predicate = str(new.get("predicate") or proposal.get("fact_type") or "发生变化").strip()
        after = new.get("object", new.get("value", new.get("statement")))
        target = str(after or new.get("detail") or subject).strip()
        if not target:
            continue
        evidence = _proposal_evidence(proposal, draft_text, target)
        if evidence:
            evidence_spans.append(evidence)
        summary = " ".join(item for item in (subject, predicate, str(after or "")) if item).strip()
        events.append(ActualEvent(
            summary=summary or target,
            event_type=str(proposal.get("fact_type") or "state_change"),
            involved_entities=[subject] if subject else [],
            evidence_spans=[evidence] if evidence else [],
            confidence=0.92 if evidence else 0.5,
        ))

    # Always retain one observable narrative event for non-empty drafts.  This
    # keeps actuals useful even for legacy cards that have no expanded contract.
    if not events:
        first = next((row for row in _sentences(draft_text) if len(row[2]) >= 8), None)
        if first:
            evidence = EvidenceSpan(
                start_char=first[0], end_char=first[1], text=first[2], label="narrative_event"
            )
            events.append(ActualEvent(
                summary=first[2][:160], event_type="narrative", evidence_spans=[evidence],
                confidence=0.65,
            ))
            evidence_spans.append(evidence)

    beats: list[BeatEvidence] = []
    for fallback_seq, beat in enumerate(contract.get("beats") or [], 1):
        summary = str(beat.get("summary") or "").strip()
        span = find_evidence_span(draft_text, summary, label="beat")
        if span:
            evidence_spans.append(span)
        beats.append(BeatEvidence(
            beat_seq=int(beat.get("seq") or fallback_seq),
            completed=span is not None,
            evidence_spans=[span] if span else [],
            confidence=0.9 if span and span.text == summary else (0.68 if span else 0.62),
        ))

    target_arc_ids = set(_strings(contract.get("arc_targets")))
    active_arcs = context.get("active_arcs") or []
    arc_progress: list[ArcProgress] = []
    for arc in active_arcs:
        arc_id = str(arc.get("id") or arc.get("title") or "").strip()
        if target_arc_ids and arc_id not in target_arc_ids and str(arc.get("title") or "") not in target_arc_ids:
            continue
        target = str(arc.get("objective") or arc.get("summary") or arc.get("title") or "").strip()
        span = find_evidence_span(draft_text, target, label="arc")
        score = text_match_score(target, span.text) if span else 0.0
        if span:
            evidence_spans.append(span)
        arc_progress.append(ArcProgress(
            arc_id=arc_id or "unknown",
            summary=target,
            progress_score=round(score, 4),
            evidence_spans=[span] if span else [],
            confidence=0.82 if span else 0.58,
        ))

    # Explicitly referenced arc ids can survive even when the active-arc row is
    # unavailable (e.g. an older partially migrated project).
    known_arc_ids = {item.arc_id for item in arc_progress}
    for arc_id in target_arc_ids - known_arc_ids:
        span = find_evidence_span(draft_text, arc_id, label="arc")
        if span:
            evidence_spans.append(span)
        arc_progress.append(ArcProgress(
            arc_id=arc_id,
            summary=arc_id,
            progress_score=text_match_score(arc_id, span.text) if span else 0.0,
            evidence_spans=[span] if span else [],
            confidence=0.75 if span else 0.5,
        ))

    forbidden_targets = _strings(contract.get("must_not_reveal"))
    forbidden_targets += _strings(contract.get("must_not_resolve"))
    forbidden_targets += _strings(contract.get("forbidden_state_changes"))
    for milestone in context.get("forbidden_reveals") or []:
        forbidden_targets.extend(_milestone_keywords(milestone))
    revealed: list[RevealEvidence] = []
    for target in dict.fromkeys(forbidden_targets):
        # Forbidden evidence is exact-only: fuzzy similarity must never invent a
        # hard violation.
        start = draft_text.find(target)
        if start < 0:
            continue
        span = EvidenceSpan(
            start_char=start,
            end_char=start + len(target),
            text=draft_text[start:start + len(target)],
            label="forbidden_reveal",
        )
        evidence_spans.append(span)
        revealed.append(RevealEvidence(
            information=target, forbidden=True, evidence_spans=[span], confidence=1.0,
        ))

    forbidden_set = set(forbidden_targets)
    for proposal in proposals:
        if proposal.get("fact_type") != "knowledge":
            continue
        new = proposal.get("new") if isinstance(proposal.get("new"), dict) else {}
        information = str(
            new.get("object") or new.get("statement") or new.get("detail") or ""
        ).strip()
        if not information or information in forbidden_set:
            continue
        span = _proposal_evidence(proposal, draft_text, information)
        if span:
            evidence_spans.append(span)
        revealed.append(RevealEvidence(
            information=information,
            forbidden=False,
            evidence_spans=[span] if span else [],
            confidence=0.9 if span else 0.5,
        ))

    state_changes = [
        change for proposal in proposals
        if (change := _proposal_state_change(proposal, draft_text)) is not None
    ]
    evidence_spans.extend(
        span for change in state_changes for span in change.evidence_spans
    )

    unresolved: list[str] = []
    for field in ("must_advance", "must_resolve", "success_criteria"):
        for target in _strings(contract.get(field)):
            if find_evidence_span(draft_text, target, threshold=0.34) is None:
                unresolved.append(target)

    actuals = ChapterActuals(
        chapter=chapter,
        draft_id=draft_id,
        events=events,
        state_changes=state_changes,
        completed_beats=beats,
        arc_progress=arc_progress,
        revealed_information=revealed,
        unresolved_promises=list(dict.fromkeys(unresolved)),
        involved_entities=list(dict.fromkeys(involved_entities)),
        evidence_spans=_dedupe_spans(evidence_spans),
        extractor_version=EXTRACTOR_VERSION,
    )
    return ActualsExtraction(actuals=actuals, proposals=proposals)


def persist_chapter_actuals(
    conn: sqlite3.Connection,
    actuals: ChapterActuals,
) -> str | None:
    """Upsert an extraction and make it the chapter's current actuals row.

    The caller owns the surrounding transaction.  Missing v20 tables degrade
    cleanly so the generation pipeline remains compatible with old databases.
    """
    if not actuals.draft_id:
        return None
    payload = actuals.model_dump(mode="json")
    version = actuals.extractor_version or EXTRACTOR_VERSION
    savepoint = "persist_chapter_actuals"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        existing = conn.execute(
            "SELECT id FROM chapter_actuals WHERE draft_id=? AND extractor_version=?",
            (actuals.draft_id, version),
        ).fetchone()
        actuals_id = existing["id"] if existing else new_id("actual")
        conn.execute(
            "UPDATE chapter_actuals SET status='superseded', superseded_at=datetime('now')"
            " WHERE chapter=? AND status='current' AND id<>?",
            (actuals.chapter, actuals_id),
        )
        if existing:
            conn.execute(
                "UPDATE chapter_actuals SET chapter=?, payload_json=?, status='current',"
                " superseded_at=NULL WHERE id=?",
                (
                    actuals.chapter,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    actuals_id,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO chapter_actuals(id,chapter,draft_id,payload_json,extractor_version,status)"
                " VALUES(?,?,?,?,?,'current')",
                (
                    actuals_id,
                    actuals.chapter,
                    actuals.draft_id,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    version,
                ),
            )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return str(actuals_id)
    except sqlite3.Error as exc:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if "no such table" in str(exc).lower():
            return None
        raise
