"""SQLite repository for decisions and future plans."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from ..craft.volume_kr import normalize_key_results
from ..ids import new_id
from .models import DecisionEnvelope, DecisionOptionDraft


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(raw: str | None, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def create_decision(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    stage: str,
    scope_type: str,
    question: str,
    options: Iterable[DecisionOptionDraft | dict],
    scope_id: str | None = None,
    context: dict[str, Any] | None = None,
    recommended_index: int = 0,
) -> dict[str, Any]:
    # LangGraph may replay a node after process failure. Reuse the already
    # committed pending decision for the same graph scope instead of creating
    # a second set of options.
    existing = conn.execute(
        "SELECT id FROM decision_sets WHERE session_id=? AND stage=? AND scope_type=?"
        " AND IFNULL(scope_id, '')=IFNULL(?, '') AND status='awaiting_choice'"
        " ORDER BY created_at DESC LIMIT 1",
        (session_id, stage, scope_type, scope_id),
    ).fetchone()
    if existing is not None:
        return load_decision(conn, existing["id"])

    decision_id = new_id("dec")
    normalized = [
        o if isinstance(o, DecisionOptionDraft) else DecisionOptionDraft.model_validate(o)
        for o in options
    ]
    if not normalized:
        raise ValueError("decision requires at least one option")
    option_ids = [new_id("opt") for _ in normalized]
    recommended_index = max(0, min(recommended_index, len(normalized) - 1))
    conn.execute(
        "INSERT INTO decision_sets(id, session_id, stage, scope_type, scope_id, question,"
        " recommended_option_id, context_json) VALUES(?,?,?,?,?,?,?,?)",
        (decision_id, session_id, stage, scope_type, scope_id, question,
         option_ids[recommended_index], _dump(context or {})),
    )
    for index, (option_id, option) in enumerate(zip(option_ids, normalized)):
        conn.execute(
            "INSERT INTO decision_options(id, decision_id, option_index, title, summary,"
            " payload_json, rationale, impact_json, score) VALUES(?,?,?,?,?,?,?,?,?)",
            (option_id, decision_id, index, option.title, option.summary,
             _dump(option.payload), option.rationale, _dump(option.impact), option.score),
        )
    conn.commit()
    return load_decision(conn, decision_id)


def patch_decision_context(
    conn: sqlite3.Connection,
    decision_id: str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Merge auditable runtime metadata into an existing decision context."""
    row = conn.execute(
        "SELECT context_json FROM decision_sets WHERE id=?", (decision_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"decision not found: {decision_id}")
    context = _load(row["context_json"], {})
    context.update(patch)
    conn.execute(
        "UPDATE decision_sets SET context_json=? WHERE id=?",
        (_dump(context), decision_id),
    )
    conn.commit()
    return load_decision(conn, decision_id)


def load_decision(conn: sqlite3.Connection, decision_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM decision_sets WHERE id=?", (decision_id,)).fetchone()
    if row is None:
        raise KeyError(f"decision not found: {decision_id}")
    options = []
    for option in conn.execute(
        "SELECT * FROM decision_options WHERE decision_id=? ORDER BY option_index",
        (decision_id,),
    ).fetchall():
        item = dict(option)
        item["payload"] = _load(item.pop("payload_json"), {})
        item["impact"] = _load(item.pop("impact_json"), {})
        item["selected"] = bool(item["selected"])
        options.append(item)
    data = dict(row)
    data["decision_id"] = data.pop("id")
    data["context"] = _load(data.pop("context_json"), {})
    data["options"] = options
    return DecisionEnvelope.model_validate(data).model_dump()


def resolve_decision(
    conn: sqlite3.Connection,
    decision_id: str,
    option_id: str,
    *,
    auto: bool = False,
) -> dict[str, Any]:
    decision_row = conn.execute(
        "SELECT status, selected_option_id FROM decision_sets WHERE id=?",
        (decision_id,),
    ).fetchone()
    if decision_row is None:
        raise KeyError(f"decision not found: {decision_id}")
    if decision_row["status"] != "awaiting_choice":
        if decision_row["selected_option_id"] == option_id:
            return load_decision(conn, decision_id)
        raise ValueError(
            f"decision {decision_id} already resolved with "
            f"{decision_row['selected_option_id']}"
        )

    row = conn.execute(
        "SELECT id FROM decision_options WHERE id=? AND decision_id=?",
        (option_id, decision_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"option {option_id} not in decision {decision_id}")
    status = "auto_selected" if auto else "selected"
    conn.execute(
        "UPDATE decision_sets SET status=?, selected_option_id=?, resolved_at=datetime('now')"
        " WHERE id=? AND status='awaiting_choice'",
        (status, option_id, decision_id),
    )
    conn.execute(
        "UPDATE decision_options SET selected=CASE WHEN id=? THEN 1 ELSE 0 END"
        " WHERE decision_id=?",
        (option_id, decision_id),
    )
    conn.commit()
    return load_decision(conn, decision_id)


def supersede_decision(conn: sqlite3.Connection, decision_id: str) -> None:
    conn.execute(
        "UPDATE decision_sets SET status='superseded', resolved_at=datetime('now')"
        " WHERE id=? AND status='awaiting_choice'",
        (decision_id,),
    )
    conn.commit()


def selected_option(decision: dict[str, Any]) -> dict[str, Any]:
    selected_id = decision.get("selected_option_id")
    option = next((o for o in decision.get("options", []) if o["id"] == selected_id), None)
    if option is None:
        raise ValueError("decision has no selected option")
    return option


def apply_story_plan(conn: sqlite3.Connection, decision: dict[str, Any]) -> str:
    existing = conn.execute(
        "SELECT id FROM story_plans WHERE source_decision_id=? LIMIT 1",
        (decision["decision_id"],),
    ).fetchone()
    if existing is not None:
        return existing["id"]

    option = selected_option(decision)
    payload = option["payload"]
    plan = payload.get("plan") or payload
    conn.execute(
        "UPDATE story_plans SET status='superseded', updated_at=datetime('now')"
        " WHERE status='active'"
    )
    version_row = conn.execute("SELECT COALESCE(MAX(version),0)+1 AS v FROM story_plans").fetchone()
    version = int(version_row["v"])
    plan_id = new_id("plan")
    conn.execute(
        "INSERT INTO story_plans(id, version, title, premise, ending_direction, phase_summary,"
        " constraints_json, status, source_decision_id) VALUES(?,?,?,?,?,?,?,'active',?)",
        (plan_id, version, plan["title"], plan.get("premise"),
         plan.get("ending_direction"), plan.get("phase_summary"),
         _dump(plan.get("constraints") or {}), decision["decision_id"]),
    )
    for arc_data in plan.get("arcs") or []:
        arc_id = new_id("arc")
        conn.execute(
            "INSERT INTO story_arcs(id, plan_id, arc_type, title, summary, objective,"
            " start_chapter, end_chapter, priority, constraints_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (arc_id, plan_id, arc_data.get("arc_type", "main"), arc_data["title"],
             arc_data["summary"], arc_data.get("objective"),
             int(arc_data.get("start_chapter") or 1), arc_data.get("end_chapter"),
             int(arc_data.get("priority") or 3), _dump(arc_data.get("constraints") or {})),
        )
        for milestone in arc_data.get("milestones") or []:
            conn.execute(
                "INSERT INTO arc_milestones(id, arc_id, title, summary, due_start_chapter,"
                " due_end_chapter, not_before_chapter, target_state_json, source_decision_id)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id("mile"), arc_id, milestone["title"], milestone["summary"],
                 milestone.get("due_start_chapter"), milestone.get("due_end_chapter"),
                 milestone.get("not_before_chapter"),
                 _dump(milestone.get("target_state") or {}), decision["decision_id"]),
            )
    conn.commit()
    return plan_id


def apply_volume_plan(conn: sqlite3.Connection, decision: dict[str, Any]) -> int:
    existing = conn.execute(
        "SELECT volume_no FROM volumes WHERE source_decision_id=? LIMIT 1",
        (decision["decision_id"],),
    ).fetchone()
    if existing is not None:
        return int(existing["volume_no"])

    option = selected_option(decision)
    payload = option["payload"]
    row = conn.execute("SELECT COALESCE(MAX(volume_no),0)+1 AS n FROM volumes").fetchone()
    volume_no = int(row["n"])
    conn.execute(
        "INSERT INTO volumes(id, volume_no, title, synopsis, start_chapter, end_chapter,"
        " objective, key_results, source_decision_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (new_id("vol"), volume_no, payload["title"], payload["synopsis"],
         int(payload["start_chapter"]), int(payload["end_chapter"]),
         payload.get("objective"),
         _dump(normalize_key_results(payload.get("key_results") or [])),
         decision["decision_id"]),
    )
    conn.commit()
    return volume_no


def apply_chapter_route(conn: sqlite3.Connection, decision: dict[str, Any]) -> list[int]:
    option = selected_option(decision)
    payload = option["payload"]
    planned: list[int] = []
    for chapter_data in payload.get("chapters") or []:
        chapter = int(chapter_data["chapter"])
        drafted = conn.execute(
            "SELECT 1 FROM draft_index WHERE chapter=? LIMIT 1", (chapter,)
        ).fetchone()
        existing = conn.execute(
            "SELECT status, locked, plan_version, source_decision_id"
            " FROM chapter_cards WHERE chapter=?", (chapter,)
        ).fetchone()
        if existing and existing["source_decision_id"] == decision["decision_id"]:
            planned.append(chapter)
            continue
        if drafted or (existing and existing["status"] != "planned"):
            continue
        next_version = int(existing["plan_version"] + 1) if existing else 1
        conn.execute(
            "INSERT INTO chapter_cards(id, chapter, title, goal, hook_text, target_emotion,"
            " opening_hook_type, hook_type, expectation_score, status, plan_version, locked,"
            " source_decision_id) VALUES(?,?,?,?,?,?,?,?,?,'planned',?,1,?)"
            " ON CONFLICT(chapter) DO UPDATE SET title=excluded.title, goal=excluded.goal,"
            " hook_text=excluded.hook_text, target_emotion=excluded.target_emotion,"
            " opening_hook_type=excluded.opening_hook_type, hook_type=excluded.hook_type,"
            " expectation_score=excluded.expectation_score, status='planned',"
            " plan_version=excluded.plan_version, locked=1,"
            " source_decision_id=excluded.source_decision_id",
            (new_id("card"), chapter, chapter_data.get("title"), chapter_data.get("goal"),
             chapter_data.get("hook_text"), chapter_data.get("target_emotion"),
             chapter_data.get("opening_hook_type"), chapter_data.get("hook_type"),
             chapter_data.get("expectation_score"), next_version, decision["decision_id"]),
        )
        conn.execute("DELETE FROM beats WHERE chapter=? AND status='planned'", (chapter,))
        for index, beat in enumerate(chapter_data.get("beats") or [], start=1):
            conn.execute(
                "INSERT INTO beats(id, chapter, seq, beat_type, summary, value_axis, arc_id,"
                " status, source_decision_id, constraint_level)"
                " VALUES(?,?,?,?,?,?,?,'planned',?,?)",
                (new_id("beat"), chapter, int(beat.get("seq") or index),
                 beat.get("beat_type", "setup"), beat.get("summary") or "推进剧情",
                 beat.get("value_axis"), beat.get("arc_id"), decision["decision_id"],
                 beat.get("constraint_level", "soft")),
            )
        planned.append(chapter)
    conn.commit()
    return planned


def story_map(conn: sqlite3.Connection) -> dict[str, Any]:
    plan_row = conn.execute(
        "SELECT * FROM story_plans WHERE status='active' ORDER BY version DESC LIMIT 1"
    ).fetchone()
    plan = dict(plan_row) if plan_row else None
    if plan:
        plan["constraints"] = _load(plan.pop("constraints_json"), {})
    arcs = []
    for row in conn.execute(
        "SELECT * FROM story_arcs WHERE status!='abandoned' ORDER BY start_chapter, priority DESC"
    ).fetchall():
        arc = dict(row)
        arc["constraints"] = _load(arc.pop("constraints_json"), {})
        arc["milestones"] = []
        arcs.append(arc)
    by_id = {a["id"]: a for a in arcs}
    for row in conn.execute(
        "SELECT * FROM arc_milestones ORDER BY COALESCE(due_start_chapter, 999999), created_at"
    ).fetchall():
        milestone = dict(row)
        milestone["target_state"] = _load(milestone.pop("target_state_json"), {})
        if milestone["arc_id"] in by_id:
            by_id[milestone["arc_id"]]["milestones"].append(milestone)
    volumes = []
    for row in conn.execute("SELECT * FROM volumes ORDER BY volume_no").fetchall():
        volume = dict(row)
        volume["key_results"] = _load(volume.get("key_results"), [])
        volumes.append(volume)
    chapters = [dict(r) for r in conn.execute(
        "SELECT chapter, title, goal, hook_text, status, plan_version, locked,"
        " source_decision_id FROM chapter_cards ORDER BY chapter"
    ).fetchall()]
    return {"plan": plan, "arcs": arcs, "volumes": volumes, "chapters": chapters}
