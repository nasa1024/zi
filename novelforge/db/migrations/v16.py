"""Migration v15 -> v16: choice-driven Agent and future story planning.

Future plans are deliberately stored outside canon/world-state tables.  They are
author intent consumed by PlanContext, while historical truth remains guarded by
the existing as-of projection.
"""
from __future__ import annotations

import sqlite3

from . import column_exists, register, table_exists


_BEATS_V16 = """
CREATE TABLE IF NOT EXISTS beats (
    id TEXT PRIMARY KEY,
    chapter INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    beat_type TEXT NOT NULL
        CHECK(beat_type IN ('setup','turn','payoff_beat','tension_point','hook')),
    summary TEXT NOT NULL,
    value_start TEXT,
    value_end TEXT,
    value_axis TEXT,
    arc_id TEXT,
    source_decision_id TEXT,
    constraint_level TEXT NOT NULL DEFAULT 'soft'
        CHECK(constraint_level IN ('soft','hard')),
    tension_level INTEGER,
    related_foreshadow_id TEXT,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK(status IN ('planned','drafted','satisfied','dropped')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chapter, seq),
    FOREIGN KEY(related_foreshadow_id) REFERENCES foreshadow(id)
);
CREATE INDEX IF NOT EXISTS idx_beats_chapter ON beats(chapter, seq);
CREATE INDEX IF NOT EXISTS idx_beats_type ON beats(beat_type);
"""


_PLANNING_DDL = r"""
CREATE TABLE IF NOT EXISTS story_plans (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    premise TEXT,
    ending_direction TEXT,
    phase_summary TEXT,
    constraints_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','superseded','archived')),
    source_decision_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(version),
    CHECK(json_valid(constraints_json))
);
CREATE INDEX IF NOT EXISTS idx_story_plans_status ON story_plans(status, version);

CREATE TABLE IF NOT EXISTS story_arcs (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES story_plans(id),
    arc_type TEXT NOT NULL DEFAULT 'main'
        CHECK(arc_type IN ('main','character','mystery','romance','foreshadow','world','other')),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    objective TEXT,
    start_chapter INTEGER NOT NULL,
    end_chapter INTEGER,
    priority INTEGER NOT NULL DEFAULT 3,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK(status IN ('planned','active','completed','deferred','abandoned')),
    constraints_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK(end_chapter IS NULL OR end_chapter >= start_chapter),
    CHECK(json_valid(constraints_json))
);
CREATE INDEX IF NOT EXISTS idx_story_arcs_window ON story_arcs(start_chapter, end_chapter, status);
CREATE INDEX IF NOT EXISTS idx_story_arcs_plan ON story_arcs(plan_id, priority);

CREATE TABLE IF NOT EXISTS arc_milestones (
    id TEXT PRIMARY KEY,
    arc_id TEXT NOT NULL REFERENCES story_arcs(id),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    due_start_chapter INTEGER,
    due_end_chapter INTEGER,
    not_before_chapter INTEGER,
    target_state_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK(status IN ('planned','active','completed','deferred','invalidated')),
    completed_chapter INTEGER,
    source_decision_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK(due_end_chapter IS NULL OR due_start_chapter IS NULL OR due_end_chapter >= due_start_chapter),
    CHECK(json_valid(target_state_json))
);
CREATE INDEX IF NOT EXISTS idx_arc_milestones_due
    ON arc_milestones(status, due_start_chapter, due_end_chapter, not_before_chapter);
CREATE INDEX IF NOT EXISTS idx_arc_milestones_arc ON arc_milestones(arc_id, status);

CREATE TABLE IF NOT EXISTS decision_sets (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    stage TEXT NOT NULL,
    scope_type TEXT NOT NULL DEFAULT 'project'
        CHECK(scope_type IN ('project','story','volume','chapter','draft','replan')),
    scope_id TEXT,
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'awaiting_choice'
        CHECK(status IN ('awaiting_choice','selected','auto_selected','superseded','canceled')),
    recommended_option_id TEXT,
    selected_option_id TEXT,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    CHECK(json_valid(context_json))
);
CREATE INDEX IF NOT EXISTS idx_decision_sets_session ON decision_sets(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_decision_sets_status ON decision_sets(status, stage);

CREATE TABLE IF NOT EXISTS decision_options (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES decision_sets(id),
    option_index INTEGER NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    rationale TEXT,
    impact_json TEXT NOT NULL DEFAULT '{}',
    score REAL,
    selected INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(decision_id, option_index),
    CHECK(json_valid(payload_json)),
    CHECK(json_valid(impact_json))
);
CREATE INDEX IF NOT EXISTS idx_decision_options_decision ON decision_options(decision_id, option_index);

CREATE TABLE IF NOT EXISTS plan_revisions (
    id TEXT PRIMARY KEY,
    plan_id TEXT REFERENCES story_plans(id),
    source_chapter INTEGER,
    reason TEXT NOT NULL,
    divergence_score REAL NOT NULL DEFAULT 0.0,
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    source_decision_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK(json_valid(before_json)),
    CHECK(json_valid(after_json))
);
CREATE INDEX IF NOT EXISTS idx_plan_revisions_plan ON plan_revisions(plan_id, created_at);
"""


@register("16")
def migrate_v16(conn: sqlite3.Connection) -> None:
    if not column_exists(conn, "sessions", "interaction_mode"):
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN interaction_mode TEXT NOT NULL "
            "DEFAULT 'guided' CHECK(interaction_mode IN ('guided','copilot','autonomous'))"
        )

    if not table_exists(conn, "beats"):
        conn.executescript(_BEATS_V16)

    for table, columns in (
        ("chapter_cards", (
            ("plan_version", "INTEGER NOT NULL DEFAULT 1"),
            ("locked", "INTEGER NOT NULL DEFAULT 0"),
            ("source_decision_id", "TEXT"),
        )),
        ("beats", (
            ("source_decision_id", "TEXT"),
            ("constraint_level", "TEXT NOT NULL DEFAULT 'soft' "
             "CHECK(constraint_level IN ('soft','hard'))"),
        )),
    ):
        for column, declaration in columns:
            if not column_exists(conn, table, column):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    conn.executescript(_PLANNING_DDL)
