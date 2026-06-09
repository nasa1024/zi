"""Branch ancestry helpers (§9.4 分支隔离 read path)."""
from __future__ import annotations

import sqlite3
from typing import Optional


def build_branch_filter(
    conn: sqlite3.Connection, branch_id: Optional[str]
) -> tuple[str, tuple]:
    """Build SQL AND-clause for branch-scoped source_fact filtering.

    Traverses the branch ancestry chain so the returned fragment (appended to a
    WHERE clause) enforces:

      * mainline facts (branch_id IS NULL)  — visible up to root branch's
        fork_chapter (inclusive)
      * ancestor-branch facts              — visible up to child's fork_chapter
      * leaf-branch facts                  — all visible, no chapter cap

    Requires the log table to be aliased ``x`` and the LEFT-JOINed facts table
    to be aliased ``f`` in the calling query::

        branch_sql, branch_params = build_branch_filter(conn, branch_id)
        cursor.execute(
            "SELECT ... FROM some_log x LEFT JOIN facts f ON f.id=x.source_fact_id"
            " WHERE ... " + branch_sql,
            base_params + branch_params,
        )

    Returns ``('', ())`` when *branch_id* is ``None`` (mainline — no extra filtering).
    """
    if branch_id is None:
        return "", ()

    # Walk ancestry: collect [(id, fork_chapter, base_branch_id)] leaf→root
    ancestry: list[tuple[str, int, Optional[str]]] = []
    current: Optional[str] = branch_id
    while current is not None:
        row = conn.execute(
            "SELECT id, base_branch_id, fork_chapter FROM branches WHERE id=?",
            (current,),
        ).fetchone()
        if not row:
            break
        ancestry.append((row["id"], row["fork_chapter"], row["base_branch_id"]))
        current = row["base_branch_id"]

    if not ancestry:
        return "", ()

    # ancestry[0] = leaf (branch_id), ancestry[-1] = root (closest to mainline)
    conditions: list[str] = []
    params: list = []

    # Mainline: visible up to root branch's fork_chapter (inclusive)
    root_fork = ancestry[-1][1]
    conditions.append("(f.branch_id IS NULL AND f.valid_from_chapter<=?)")
    params.append(root_fork)

    # Ancestor branches (root→leaf order, excluding leaf)
    for i in range(len(ancestry) - 1, 0, -1):
        bid = ancestry[i][0]
        child_fork = ancestry[i - 1][1]
        conditions.append("(f.branch_id=? AND f.valid_from_chapter<=?)")
        params.extend([bid, child_fork])

    # Leaf branch: all facts, no chapter cap
    conditions.append("(f.branch_id=?)")
    params.append(branch_id)

    sql = "AND (x.source_fact_id IS NULL OR (" + " OR ".join(conditions) + "))"
    return sql, tuple(params)
