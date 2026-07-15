"""Migration v19 -> v20: versioned chapter contracts and plan reconciliation.

Future plans remain author intent rather than canon.  ``chapter_cards`` keeps a
small current projection for compatibility, while append-only plan versions,
actual chapter outcomes, and alignment reports retain the auditable history.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import column_exists, register, table_exists


_V20_TABLES = """
CREATE TABLE IF NOT EXISTS chapter_plan_versions (
    id                         TEXT PRIMARY KEY,
    chapter                    INTEGER NOT NULL,
    version                    INTEGER NOT NULL CHECK(version >= 1),
    payload_json               TEXT NOT NULL DEFAULT '{}',
    status                     TEXT NOT NULL DEFAULT 'candidate'
                                   CHECK(status IN ('candidate','active','superseded',
                                                    'executed','abandoned')),
    source_decision_id         TEXT,
    source_alignment_report_id TEXT,
    based_on_chapter           INTEGER,
    reason                     TEXT,
    divergence_score           REAL
                                   CHECK(divergence_score IS NULL OR
                                         (divergence_score >= 0.0 AND divergence_score <= 1.0)),
    created_at                 TEXT NOT NULL DEFAULT (datetime('now')),
    activated_at               TEXT,
    superseded_at              TEXT,
    UNIQUE(chapter, version),
    CHECK(json_valid(payload_json))
);

CREATE TABLE IF NOT EXISTS chapter_plan_version_beats (
    id               TEXT PRIMARY KEY,
    plan_version_id  TEXT NOT NULL REFERENCES chapter_plan_versions(id),
    seq              INTEGER NOT NULL CHECK(seq >= 1),
    beat_type        TEXT NOT NULL
                         CHECK(beat_type IN ('setup','turn','payoff_beat',
                                             'tension_point','hook')),
    summary          TEXT NOT NULL,
    value_axis       TEXT,
    arc_id           TEXT,
    constraint_level TEXT NOT NULL DEFAULT 'soft'
                         CHECK(constraint_level IN ('soft','hard')),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(plan_version_id, seq)
);

CREATE TABLE IF NOT EXISTS chapter_actuals (
    id                TEXT PRIMARY KEY,
    chapter           INTEGER NOT NULL,
    draft_id          TEXT NOT NULL REFERENCES draft_index(id),
    payload_json      TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'current'
                          CHECK(status IN ('current','superseded')),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_at     TEXT,
    UNIQUE(draft_id, extractor_version),
    CHECK(json_valid(payload_json))
);

CREATE TABLE IF NOT EXISTS plan_alignment_reports (
    id                TEXT PRIMARY KEY,
    chapter           INTEGER NOT NULL,
    plan_version_id   TEXT REFERENCES chapter_plan_versions(id),
    actuals_id         TEXT NOT NULL REFERENCES chapter_actuals(id),
    goal_score         REAL NOT NULL CHECK(goal_score >= 0.0 AND goal_score <= 1.0),
    beat_score         REAL NOT NULL CHECK(beat_score >= 0.0 AND beat_score <= 1.0),
    arc_score          REAL NOT NULL CHECK(arc_score >= 0.0 AND arc_score <= 1.0),
    volume_score       REAL NOT NULL CHECK(volume_score >= 0.0 AND volume_score <= 1.0),
    reveal_score       REAL NOT NULL CHECK(reveal_score >= 0.0 AND reveal_score <= 1.0),
    divergence_score   REAL NOT NULL
                           CHECK(divergence_score >= 0.0 AND divergence_score <= 1.0),
    divergence_level   TEXT NOT NULL
                           CHECK(divergence_level IN ('normal','minor','major','severe')),
    confidence         REAL CHECK(confidence IS NULL OR
                                   (confidence >= 0.0 AND confidence <= 1.0)),
    decision           TEXT CHECK(decision IS NULL OR
                                  decision IN ('keep','patch','replan','pause')),
    detail_json        TEXT NOT NULL DEFAULT '{}',
    evaluator_version  TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'current'
                           CHECK(status IN ('current','superseded')),
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_at      TEXT,
    UNIQUE(plan_version_id, actuals_id, evaluator_version),
    CHECK(json_valid(detail_json))
);
"""


_V20_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_chapter_plan_versions_chapter
    ON chapter_plan_versions(chapter, version DESC);
CREATE INDEX IF NOT EXISTS idx_chapter_plan_versions_status
    ON chapter_plan_versions(chapter, status);
CREATE INDEX IF NOT EXISTS idx_chapter_plan_versions_source_decision
    ON chapter_plan_versions(source_decision_id);
CREATE INDEX IF NOT EXISTS idx_chapter_plan_versions_source_alignment
    ON chapter_plan_versions(source_alignment_report_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chapter_plan_versions_one_active
    ON chapter_plan_versions(chapter) WHERE status='active';

CREATE INDEX IF NOT EXISTS idx_chapter_plan_version_beats_version
    ON chapter_plan_version_beats(plan_version_id, seq);
CREATE INDEX IF NOT EXISTS idx_chapter_plan_version_beats_arc
    ON chapter_plan_version_beats(arc_id);

CREATE INDEX IF NOT EXISTS idx_chapter_actuals_chapter
    ON chapter_actuals(chapter, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chapter_actuals_draft
    ON chapter_actuals(draft_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chapter_actuals_one_current
    ON chapter_actuals(chapter) WHERE status='current';

CREATE INDEX IF NOT EXISTS idx_plan_alignment_reports_chapter
    ON plan_alignment_reports(chapter, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_plan_alignment_reports_plan_version
    ON plan_alignment_reports(plan_version_id);
CREATE INDEX IF NOT EXISTS idx_plan_alignment_reports_actuals
    ON plan_alignment_reports(actuals_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_alignment_reports_one_current
    ON plan_alignment_reports(chapter) WHERE status='current';
"""


_CHAPTER_CONTRACT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("arc_targets_json", "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(arc_targets_json))"),
    ("volume_kr_targets_json", "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(volume_kr_targets_json))"),
    ("milestone_targets_json", "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(milestone_targets_json))"),
    ("must_advance_json", "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(must_advance_json))"),
    ("must_resolve_json", "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(must_resolve_json))"),
    ("must_include_entities_json", "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(must_include_entities_json))"),
    ("must_not_reveal_json", "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(must_not_reveal_json))"),
    ("must_not_resolve_json", "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(must_not_resolve_json))"),
    ("forbidden_state_changes_json", "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(forbidden_state_changes_json))"),
    ("target_state_json", "TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(target_state_json))"),
    ("success_criteria_json", "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(success_criteria_json))"),
    ("current_plan_version_id", "TEXT REFERENCES chapter_plan_versions(id)"),
)


def _execute_transactional_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute this migration's simple DDL without ``executescript``.

    Python's sqlite3 ``executescript`` issues an implicit COMMIT before the
    script, which makes a later migration failure impossible to roll back.
    v20 contains no triggers or semicolons inside statements, so executing the
    statements individually preserves the surrounding transaction.
    """
    for raw_statement in script.split(";"):
        statement = raw_statement.strip()
        if statement:
            conn.execute(statement)


def _json_value(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback
    return value


def _backfill_current_plan_versions(conn: sqlite3.Connection) -> None:
    """Snapshot legacy chapter_cards/beats without changing their projection."""
    if not table_exists(conn, "chapter_cards"):
        return
    rows = conn.execute(
        "SELECT chapter, title, goal, hook_text, target_emotion, opening_hook_type,"
        " hook_type, expectation_score, status, plan_version, locked, source_decision_id,"
        " arc_targets_json, volume_kr_targets_json, milestone_targets_json,"
        " must_advance_json, must_resolve_json, must_include_entities_json,"
        " must_not_reveal_json, must_not_resolve_json, forbidden_state_changes_json,"
        " target_state_json, success_criteria_json, current_plan_version_id"
        " FROM chapter_cards ORDER BY chapter"
    ).fetchall()
    for row in rows:
        if row["current_plan_version_id"]:
            continue
        chapter = int(row["chapter"])
        version = max(1, int(row["plan_version"] or 1))
        existing = conn.execute(
            "SELECT id FROM chapter_plan_versions WHERE chapter=? AND version=?",
            (chapter, version),
        ).fetchone()
        version_id = existing["id"] if existing else f"cpv_legacy_{chapter}_{version}"
        if existing is None:
            payload = {
                "chapter": chapter,
                "title": row["title"],
                "goal": row["goal"] or "",
                "hook_text": row["hook_text"],
                "target_emotion": row["target_emotion"],
                "opening_hook_type": row["opening_hook_type"],
                "hook_type": row["hook_type"],
                "expectation_score": row["expectation_score"],
                "arc_targets": _json_value(row["arc_targets_json"], []),
                "volume_kr_targets": _json_value(row["volume_kr_targets_json"], []),
                "milestone_targets": _json_value(row["milestone_targets_json"], []),
                "must_advance": _json_value(row["must_advance_json"], []),
                "must_resolve": _json_value(row["must_resolve_json"], []),
                "must_include_entities": _json_value(
                    row["must_include_entities_json"], []
                ),
                "must_not_reveal": _json_value(row["must_not_reveal_json"], []),
                "must_not_resolve": _json_value(row["must_not_resolve_json"], []),
                "forbidden_state_changes": _json_value(
                    row["forbidden_state_changes_json"], []
                ),
                "target_state": _json_value(row["target_state_json"], {}),
                "success_criteria": _json_value(row["success_criteria_json"], []),
            }
            version_status = "active" if row["status"] == "planned" else "executed"
            conn.execute(
                "INSERT INTO chapter_plan_versions("
                " id, chapter, version, payload_json, status, source_decision_id,"
                " reason, activated_at) VALUES(?,?,?,?,?,?,?,datetime('now'))",
                (
                    version_id,
                    chapter,
                    version,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    version_status,
                    row["source_decision_id"],
                    "v20_legacy_backfill",
                ),
            )

            beats = (
                conn.execute(
                    "SELECT id, seq, beat_type, summary, value_axis, arc_id,"
                    " constraint_level FROM beats WHERE chapter=? ORDER BY seq",
                    (chapter,),
                ).fetchall()
                if table_exists(conn, "beats")
                else []
            )
            for beat in beats:
                conn.execute(
                    "INSERT OR IGNORE INTO chapter_plan_version_beats("
                    " id, plan_version_id, seq, beat_type, summary, value_axis, arc_id,"
                    " constraint_level) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        f"cpvb_legacy_{beat['id']}",
                        version_id,
                        beat["seq"],
                        beat["beat_type"],
                        beat["summary"],
                        beat["value_axis"],
                        beat["arc_id"],
                        beat["constraint_level"] or "soft",
                    ),
                )
        conn.execute(
            "UPDATE chapter_cards SET current_plan_version_id=? WHERE chapter=?",
            (version_id, chapter),
        )


@register("20")
def migrate_v20(conn: sqlite3.Connection) -> None:
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        _execute_transactional_script(conn, _V20_TABLES)

        # ``source_alignment_report_id`` was added late in the v20 design.
        # Keep this guard so partially-created development DBs are repairable.
        if not column_exists(
            conn, "chapter_plan_versions", "source_alignment_report_id"
        ):
            conn.execute(
                "ALTER TABLE chapter_plan_versions"
                " ADD COLUMN source_alignment_report_id TEXT"
            )

        if table_exists(conn, "chapter_cards"):
            for column, declaration in _CHAPTER_CONTRACT_COLUMNS:
                if not column_exists(conn, "chapter_cards", column):
                    conn.execute(
                        f"ALTER TABLE chapter_cards ADD COLUMN {column} {declaration}"
                    )

        _execute_transactional_script(conn, _V20_INDEXES)
        _backfill_current_plan_versions(conn)
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise
