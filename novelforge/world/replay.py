"""Read-side as-of replay functions (§16.9).

Eager / defensive: LEFT JOIN facts ON source_fact_id filters status='canon' + valid interval.
Even if a retcon cascade was interrupted (crash/dirty data), replay_* won't include stale rows.

get_world_state(as_of_chapter, conn, branch_id=None) is the public entry point — returns a
WorldState that lazily queries *_log tables on demand.

Branch isolation (§9.4 Group 13):
  Pass branch_id to scope projections to a branch's ancestry chain.
  branch_id=None → mainline behaviour (no extra filtering).
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from ..validators.types import WorldState
from .branch import build_branch_filter

# Shared AS-OF filter: binds (N, N) for the two inequality checks.
_AS_OF = (
    "(x.source_fact_id IS NULL OR"
    " (f.status='canon' AND f.valid_from_chapter<=?"
    "  AND (f.valid_to_chapter IS NULL OR f.valid_to_chapter>?)))"
)


def get_world_state(
    as_of_chapter: int,
    conn: sqlite3.Connection,
    branch_id: Optional[str] = None,
) -> WorldState:
    """Factory: return a WorldState snapshot lazily backed by *_log tables."""
    return WorldState(as_of=as_of_chapter, conn=conn, branch_id=branch_id)


def replay_power(
    conn: sqlite3.Connection, N: int, branch_id: Optional[str] = None
) -> dict:
    """Returns {entity_id: rank_order} — latest power state as of chapter N."""
    branch_sql, branch_params = build_branch_filter(conn, branch_id)
    rows = conn.execute(
        "SELECT x.entity_id, x.rank_order, x.change_chapter"
        " FROM character_power_log x"
        " LEFT JOIN facts f ON f.id=x.source_fact_id"
        f" WHERE x.change_chapter<=? AND {_AS_OF}"
        f" {branch_sql}"
        " ORDER BY x.entity_id, x.change_chapter",
        (N, N, N) + branch_params,
    ).fetchall()
    out: dict = {}
    for r in rows:
        out[r["entity_id"]] = r["rank_order"]
    return out


def replay_knowledge(
    conn: sqlite3.Connection, N: int, branch_id: Optional[str] = None
) -> dict:
    """Returns {knower_entity_id: {secret_key: knowledge_state}} as of chapter N."""
    branch_sql, branch_params = build_branch_filter(conn, branch_id)
    rows = conn.execute(
        "SELECT x.knower_entity_id, x.secret_key, x.knowledge_state"
        " FROM knowledge_edges x"
        " LEFT JOIN facts f ON f.id=x.source_fact_id"
        f" WHERE x.learned_chapter<=? AND {_AS_OF}"
        f" {branch_sql}"
        " ORDER BY x.knower_entity_id, x.secret_key, x.learned_chapter",
        (N, N, N) + branch_params,
    ).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["knower_entity_id"], {})[r["secret_key"]] = r["knowledge_state"]
    return out


def replay_items(
    conn: sqlite3.Connection, N: int, branch_id: Optional[str] = None
) -> dict:
    """Returns {(owner_entity_id, item_entity_id): quantity} — folded ownership as of N."""
    branch_sql, branch_params = build_branch_filter(conn, branch_id)
    rows = conn.execute(
        "SELECT x.item_entity_id, x.from_owner_id, x.to_owner_id,"
        "       x.quantity_delta, x.change_type"
        " FROM item_log x"
        " LEFT JOIN facts f ON f.id=x.source_fact_id"
        f" WHERE x.change_chapter<=? AND {_AS_OF}"
        f" {branch_sql}"
        " ORDER BY x.item_entity_id, x.change_chapter",
        (N, N, N) + branch_params,
    ).fetchall()
    owner: dict = {}
    qty: dict = {}
    for r in rows:
        it = r["item_entity_id"]
        if r["change_type"] in ("acquire", "craft"):
            owner[it] = r["to_owner_id"]
            qty[it] = qty.get(it, 0) + abs(r["quantity_delta"])
        elif r["change_type"] == "transfer":
            owner[it] = r["to_owner_id"]
        else:  # consume / destroy / lose
            qty[it] = max(qty.get(it, 0) - abs(r["quantity_delta"]), 0)
            if qty[it] == 0:
                owner[it] = None
    return {(owner.get(it), it): qty.get(it, 0) for it in qty}


def replay_numeric(
    conn: sqlite3.Connection, N: int, branch_id: Optional[str] = None
) -> dict:
    """Returns {(entity_id, metric_key): {value, unit}} — latest numeric as of N."""
    branch_sql, branch_params = build_branch_filter(conn, branch_id)
    rows = conn.execute(
        "SELECT x.entity_id, x.metric_key, x.value, x.unit"
        " FROM numeric_facts x"
        " LEFT JOIN facts f ON f.id=x.source_fact_id"
        f" WHERE x.as_of_chapter<=? AND {_AS_OF}"
        f" {branch_sql}"
        " ORDER BY x.entity_id, x.metric_key, x.as_of_chapter",
        (N, N, N) + branch_params,
    ).fetchall()
    out: dict = {}
    for r in rows:
        out[(r["entity_id"], r["metric_key"])] = {"value": r["value"], "unit": r["unit"]}
    return out


def replay_gimmick(
    conn: sqlite3.Connection, N: int, branch_id: Optional[str] = None
) -> dict:
    """Returns {gimmick_name: {id, cooldown, last_use}} as of chapter N."""
    branch_sql, branch_params = build_branch_filter(conn, branch_id)
    rules = conn.execute(
        "SELECT x.id, x.gimmick_name, x.cooldown_chapters"
        " FROM gimmick_rules x"
        " LEFT JOIN facts f ON f.id=x.source_fact_id"
        f" WHERE x.valid_from_chapter<=? AND {_AS_OF}"
        f" {branch_sql}",
        (N, N, N) + branch_params,
    ).fetchall()
    out = {
        r["gimmick_name"]: {"id": r["id"], "cooldown": r["cooldown_chapters"], "last_use": None}
        for r in rules
    }
    uses = conn.execute(
        "SELECT g.gimmick_name, MAX(x.use_chapter) AS last_use"
        " FROM gimmick_usage_log x"
        " JOIN gimmick_rules g ON g.id=x.gimmick_id"
        " LEFT JOIN facts f ON f.id=x.source_fact_id"
        f" WHERE x.use_chapter<=? AND {_AS_OF}"
        f" {branch_sql}"
        " GROUP BY g.gimmick_name",
        (N, N, N) + branch_params,
    ).fetchall()
    for u in uses:
        if u["gimmick_name"] in out:
            out[u["gimmick_name"]]["last_use"] = u["last_use"]
    return out


def load_timeline(
    conn: sqlite3.Connection,
    N: int | None = None,
    branch_id: Optional[str] = None,
) -> list:
    """All canon timeline_events, optionally filtered to chapter <= N."""
    branch_sql, branch_params = build_branch_filter(conn, branch_id)
    sql = (
        "SELECT x.* FROM timeline_events x"
        " LEFT JOIN facts f ON f.id=x.source_fact_id"
        " WHERE (x.source_fact_id IS NULL OR f.status='canon')"
    )
    params: tuple = ()
    if N is not None:
        sql += " AND x.chapter<=?"
        params = (N,)
    if branch_sql:
        sql += " " + branch_sql
        params = params + branch_params
    return conn.execute(sql + " ORDER BY x.story_time_start", params).fetchall()
