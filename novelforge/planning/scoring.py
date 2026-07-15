"""Evidence-backed plan fidelity and post-draft alignment scoring."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from ..ids import new_id
from .actuals import find_evidence_span, text_match_score
from .context import PlanContext
from .models import AlignmentFinding, ChapterActuals, EvidenceSpan, PlanAlignmentReport


EVALUATOR_VERSION = "weighted-evidence-v1"
_ALIGNMENT_WEIGHTS = {
    "goal": 0.30,
    "beat": 0.25,
    "volume": 0.15,
    "arc": 0.15,
    "reveal": 0.15,
}


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
    output: list[str] = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("text") or item.get("title") or item.get("summary") or item.get("id")
        text = str(item or "").strip()
        if text:
            output.append(text)
    return output


def _mean(values: Iterable[float], *, default: float = 1.0) -> float:
    rows = list(values)
    return round(sum(rows) / len(rows), 4) if rows else default


def _weighted_mean(values: Iterable[tuple[float, float]], *, default: float = 1.0) -> float:
    rows = list(values)
    total_weight = sum(weight for _, weight in rows)
    if not rows or total_weight <= 0:
        return default
    return round(sum(score * weight for score, weight in rows) / total_weight, 4)


def _target_score(draft_text: str, target: str) -> tuple[float, EvidenceSpan | None]:
    span = find_evidence_span(draft_text, target)
    if span is None:
        return 0.0, None
    return round(text_match_score(target, span.text), 4), span


def _milestone_keywords(milestone: dict[str, Any]) -> list[str]:
    target_state = milestone.get("target_state") or {}
    return _strings(target_state.get("keywords"))


def _forbidden_targets(context: dict[str, Any], contract: dict[str, Any]) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for field, code in (
        ("must_not_reveal", "MUST_NOT_REVEAL"),
        ("must_not_resolve", "MUST_NOT_RESOLVE"),
        ("forbidden_state_changes", "FORBIDDEN_STATE_CHANGE"),
    ):
        targets.extend((code, item) for item in _strings(contract.get(field)))
    for milestone in context.get("forbidden_reveals") or []:
        targets.extend(("PREMATURE_PLAN_REVEAL", item) for item in _milestone_keywords(milestone))
    return list(dict.fromkeys(targets))


def _goal_targets(contract: dict[str, Any]) -> list[tuple[str, float]]:
    targets: list[tuple[str, float]] = []
    goal = str(contract.get("goal") or contract.get("summary") or "").strip()
    if goal:
        targets.append((goal, 2.0))
    for field in ("must_advance", "must_resolve", "success_criteria"):
        targets.extend((item, 1.0) for item in _strings(contract.get(field)))
    targets.extend((item, 0.75) for item in _strings(contract.get("must_include_entities")))
    return targets


def _contract_targets(contract: dict[str, Any]) -> list[tuple[str, float]]:
    targets = _goal_targets(contract)
    for beat in contract.get("beats") or []:
        if beat.get("constraint_level") == "hard" and beat.get("summary"):
            targets.append((str(beat["summary"]), 1.5))
    return targets


def _arc_targets(
    context: dict[str, Any], contract: dict[str, Any],
) -> list[tuple[str, str]]:
    requested = set(_strings(contract.get("arc_targets")))
    if not requested:
        return []
    targets: list[tuple[str, str]] = []
    matched: set[str] = set()
    for arc in context.get("active_arcs") or []:
        arc_id = str(arc.get("id") or arc.get("title") or "").strip()
        title = str(arc.get("title") or "").strip()
        if arc_id not in requested and title not in requested:
            continue
        target = str(arc.get("objective") or arc.get("summary") or title or arc_id).strip()
        targets.append((arc_id or title, target))
        matched.update({arc_id, title})
    targets.extend((item, item) for item in requested - matched)
    return targets


def evaluate_candidate_against_plan(
    draft_text: str,
    *,
    chapter_contract: dict[str, Any] | None = None,
    plan_context: PlanContext | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministically evaluate a candidate before literary judging.

    Hard plan blocks require exact evidence.  Completion scores may use
    conservative sentence-level term matching and retain the matching spans.
    """
    context = _context_dict(plan_context)
    contract = chapter_contract or context.get("current_contract") or {}

    plan_blocks: list[dict[str, Any]] = []
    for code, target in _forbidden_targets(context, contract):
        start = draft_text.find(target)
        if start < 0:
            continue
        plan_blocks.append({
            "source": "plan",
            "code": code,
            "severity": "block",
            "target": target,
            "evidence": [{
                "start_char": start,
                "end_char": start + len(target),
                "text": draft_text[start:start + len(target)],
            }],
            "message": f"正文触发计划禁止项：{target}",
        })

    contract_rows: list[tuple[float, float]] = []
    contract_evidence: list[dict[str, Any]] = []
    for target, weight in _contract_targets(contract):
        score, span = _target_score(draft_text, target)
        contract_rows.append((score, weight))
        contract_evidence.append({
            "target": target,
            "score": score,
            "evidence_spans": [span.model_dump(mode="json")] if span else [],
        })
    contract_score = _weighted_mean(contract_rows)

    arc_rows: list[float] = []
    arc_evidence: list[dict[str, Any]] = []
    for arc_id, target in _arc_targets(context, contract):
        score, span = _target_score(draft_text, target)
        arc_rows.append(score)
        arc_evidence.append({
            "arc_id": arc_id,
            "target": target,
            "score": score,
            "evidence_spans": [span.model_dump(mode="json")] if span else [],
        })
    arc_score = _mean(arc_rows)
    plan_fidelity = round(0.7 * contract_score + 0.3 * arc_score, 4)
    if plan_blocks:
        plan_fidelity = 0.0
    return {
        "plan_blocks": plan_blocks,
        "contract_score": contract_score,
        "arc_progress_score": arc_score,
        "plan_fidelity": plan_fidelity,
        "contract_evidence": contract_evidence,
        "arc_evidence": arc_evidence,
    }


def _volume_targets(context: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    volume = context.get("current_volume") or {}
    targets: list[str] = []
    objective = str(volume.get("objective") or "").strip()
    if objective:
        targets.append(objective)

    requested = set(_strings(contract.get("volume_kr_targets")))
    key_results = volume.get("key_results") or []
    for item in key_results:
        if isinstance(item, dict):
            identifiers = {
                str(item.get("id") or "").strip(),
                str(item.get("title") or "").strip(),
                str(item.get("text") or "").strip(),
            }
            text = str(item.get("text") or item.get("title") or item.get("id") or "").strip()
            status = str(item.get("status") or "pending")
            if text and status != "completed" and (not requested or requested & identifiers):
                targets.append(text)
        else:
            text = str(item or "").strip()
            if text and (not requested or text in requested):
                targets.append(text)
    # Preserve explicit KR references even when a legacy volume stores no KR objects.
    targets.extend(item for item in requested if item not in targets)
    return list(dict.fromkeys(targets))


def _finding(
    category: str,
    target: str,
    score: float,
    span: EvidenceSpan | None,
    *,
    confidence: float = 0.75,
    explanation: str = "",
) -> AlignmentFinding:
    return AlignmentFinding(
        category=category,
        target=target,
        matched=score >= 0.5,
        score=round(max(0.0, min(1.0, score)), 4),
        explanation=explanation,
        evidence_spans=[span] if span else [],
        confidence=confidence if span else min(confidence, 0.6),
    )


def divergence_level(score: float) -> str:
    if score <= 0.15:
        return "normal"
    if score <= 0.30:
        return "minor"
    if score <= 0.50:
        return "major"
    return "severe"


def recommend_plan_action(
    divergence: float | PlanAlignmentReport,
    *,
    interaction_mode: str = "guided",
    hard_reveal: bool | None = None,
) -> str:
    """Map evidence-backed divergence to the mode-specific next action."""
    if isinstance(divergence, PlanAlignmentReport):
        report = divergence
        hard_reveal = bool(report.detail.get("hard_reveal")) if hard_reveal is None else hard_reveal
        value = report.divergence_score
    else:
        value = float(divergence)
    if hard_reveal:
        return "pause"
    if interaction_mode in {"autonomous", "auto_promote"}:
        if value < 0.20:
            return "keep"
        if value < 0.40:
            return "replan"
        return "pause"
    if interaction_mode in {"copilot", "hybrid"}:
        if value < 0.15:
            return "keep"
        if value <= 0.30:
            return "patch"
        return "replan"
    if value <= 0.15:
        return "keep"
    if value <= 0.30:
        return "patch"
    if value <= 0.50:
        return "replan"
    return "pause"


def score_plan_alignment(
    actuals: ChapterActuals,
    plan_context: PlanContext | dict[str, Any] | None,
    *,
    draft_text: str = "",
    interaction_mode: str = "guided",
    actuals_id: str | None = None,
) -> PlanAlignmentReport:
    """Score plan versus actuals using the documented 30/25/15/15/15 weights."""
    context = _context_dict(plan_context)
    contract = context.get("current_contract") or {}
    findings: list[AlignmentFinding] = []

    goal_rows: list[tuple[float, float]] = []
    for target, weight in _goal_targets(contract):
        score, span = _target_score(draft_text, target)
        goal_rows.append((score, weight))
        findings.append(_finding(
            "goal", target, score, span,
            explanation="正文证据支持本章目标/完成标准" if span else "未找到足够正文证据",
        ))
    goal_score = _weighted_mean(goal_rows)

    actual_beats = {item.beat_seq: item for item in actuals.completed_beats}
    beat_rows: list[tuple[float, float]] = []
    for fallback_seq, beat in enumerate(contract.get("beats") or [], 1):
        seq = int(beat.get("seq") or fallback_seq)
        target = str(beat.get("summary") or f"beat {seq}")
        evidence = actual_beats.get(seq)
        score = 1.0 if evidence and evidence.completed else 0.0
        weight = 2.0 if beat.get("constraint_level") == "hard" else 1.0
        beat_rows.append((score, weight))
        span = evidence.evidence_spans[0] if evidence and evidence.evidence_spans else None
        findings.append(_finding(
            "beat", target, score, span,
            confidence=evidence.confidence if evidence else 0.55,
            explanation="节拍已完成" if score else "节拍缺少完成证据",
        ))
    beat_score = _weighted_mean(beat_rows)

    arc_by_id = {item.arc_id: item for item in actuals.arc_progress}
    arc_rows: list[float] = []
    for arc_id, target in _arc_targets(context, contract):
        progress = arc_by_id.get(arc_id)
        if progress is not None:
            score = progress.progress_score
            span = progress.evidence_spans[0] if progress.evidence_spans else None
            confidence = progress.confidence
        else:
            score, span = _target_score(draft_text, target)
            confidence = 0.72
        arc_rows.append(score)
        findings.append(_finding(
            "arc", target, score, span, confidence=confidence,
            explanation="正文推进了目标情节弧" if span else "未找到情节弧推进证据",
        ))
    arc_score = _mean(arc_rows)

    volume_rows: list[float] = []
    for target in _volume_targets(context, contract):
        score, span = _target_score(draft_text, target)
        volume_rows.append(score)
        findings.append(_finding(
            "volume", target, score, span,
            explanation="正文服务当前卷 Objective/KR" if span else "未找到卷目标推进证据",
        ))
    volume_score = _mean(volume_rows)

    forbidden = _forbidden_targets(context, contract)
    reveal_hits = 0
    for _code, target in forbidden:
        start = draft_text.find(target)
        if start >= 0:
            reveal_hits += 1
            span = EvidenceSpan(
                start_char=start,
                end_char=start + len(target),
                text=draft_text[start:start + len(target)],
                label="forbidden_reveal",
            )
            findings.append(_finding(
                "reveal", target, 0.0, span, confidence=1.0,
                explanation="正文命中禁止提前揭示/解决项",
            ))
        else:
            findings.append(_finding(
                "reveal", target, 1.0, None, confidence=1.0,
                explanation="正文未触发禁止项",
            ))
    reveal_score = 0.0 if reveal_hits else 1.0

    alignment = (
        _ALIGNMENT_WEIGHTS["goal"] * goal_score
        + _ALIGNMENT_WEIGHTS["beat"] * beat_score
        + _ALIGNMENT_WEIGHTS["volume"] * volume_score
        + _ALIGNMENT_WEIGHTS["arc"] * arc_score
        + _ALIGNMENT_WEIGHTS["reveal"] * reveal_score
    )
    divergence = round(max(0.0, min(1.0, 1.0 - alignment)), 4)
    confidences = [finding.confidence for finding in findings]
    confidence = _mean(confidences, default=0.7)
    contract_version_id = contract.get("current_plan_version_id")
    if not contract_version_id:
        contract_version_id = contract.get("plan_version_id")
    action = recommend_plan_action(
        divergence,
        interaction_mode=interaction_mode,
        hard_reveal=reveal_hits > 0,
    )
    return PlanAlignmentReport(
        chapter=actuals.chapter,
        plan_version_id=contract_version_id,
        actuals_id=actuals_id,
        goal_score=goal_score,
        beat_score=beat_score,
        arc_score=arc_score,
        volume_score=volume_score,
        reveal_score=reveal_score,
        divergence_score=divergence,
        divergence_level=divergence_level(divergence),
        confidence=confidence,
        decision=action,
        findings=findings,
        detail={
            "weights": _ALIGNMENT_WEIGHTS,
            "hard_reveal": reveal_hits > 0,
            "unresolved_promises": actuals.unresolved_promises,
            "evidence_span_count": len(actuals.evidence_spans),
            "interaction_mode": interaction_mode,
        },
        evaluator_version=EVALUATOR_VERSION,
    )


def persist_alignment_report(
    conn: sqlite3.Connection,
    report: PlanAlignmentReport,
) -> str | None:
    """Persist a report as the current chapter authority without committing."""
    if not report.actuals_id:
        return None
    version = report.evaluator_version or EVALUATOR_VERSION
    savepoint = "persist_alignment_report"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        if report.plan_version_id is None:
            existing = conn.execute(
                "SELECT id FROM plan_alignment_reports WHERE plan_version_id IS NULL"
                " AND actuals_id=? AND evaluator_version=?",
                (report.actuals_id, version),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT id FROM plan_alignment_reports WHERE plan_version_id=?"
                " AND actuals_id=? AND evaluator_version=?",
                (report.plan_version_id, report.actuals_id, version),
            ).fetchone()
        report_id = existing["id"] if existing else new_id("align")
        conn.execute(
            "UPDATE plan_alignment_reports SET status='superseded',"
            " superseded_at=datetime('now') WHERE chapter=? AND status='current' AND id<>?",
            (report.chapter, report_id),
        )
        detail = report.model_dump(mode="json")
        values = (
            report.chapter,
            report.plan_version_id,
            report.actuals_id,
            report.goal_score,
            report.beat_score,
            report.arc_score,
            report.volume_score,
            report.reveal_score,
            report.divergence_score,
            report.divergence_level,
            report.confidence,
            report.decision,
            json.dumps(detail, ensure_ascii=False, separators=(",", ":")),
            version,
        )
        if existing:
            conn.execute(
                "UPDATE plan_alignment_reports SET chapter=?,plan_version_id=?,actuals_id=?,"
                " goal_score=?,beat_score=?,arc_score=?,volume_score=?,reveal_score=?,"
                " divergence_score=?,divergence_level=?,confidence=?,decision=?,detail_json=?,"
                " evaluator_version=?,status='current',superseded_at=NULL WHERE id=?",
                (*values, report_id),
            )
        else:
            conn.execute(
                "INSERT INTO plan_alignment_reports("
                " id,chapter,plan_version_id,actuals_id,goal_score,beat_score,arc_score,"
                " volume_score,reveal_score,divergence_score,divergence_level,confidence,"
                " decision,detail_json,evaluator_version,status)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'current')",
                (report_id, *values),
            )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return str(report_id)
    except sqlite3.Error as exc:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if "no such table" in str(exc).lower():
            return None
        raise
