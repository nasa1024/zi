"""commit_canon: atomic promotion — writes facts + fact_revisions + promotion_log + *_log.

Replaces §11.7 original four-step with a fifth projection step.
Design: §16.6.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from ..contracts import (
    HARD_FACET,
    BibleChangeProposal,
    OptimisticLockError,
    StateTransition,
)
from ..ids import new_id
from ..world.projection import ProjectionError, apply_state_transition, resolve_entity_id


@dataclass
class _FactCursor:
    id: str
    entity_id: str | None
    object: str
    status: str
    version: int
    valid_from_chapter: int
    revision_no: int


def _read_fact(conn: sqlite3.Connection, fact_id: str) -> _FactCursor:
    """facts 无 revision_no 列——经 fact_revisions 派生当前修订号。"""
    f = conn.execute(
        "SELECT id, entity_id, object, status, version, valid_from_chapter"
        " FROM facts WHERE id=?",
        (fact_id,),
    ).fetchone()
    if f is None:
        raise ProjectionError(f"facts 无此行: {fact_id}")
    rn = conn.execute(
        "SELECT COALESCE(MAX(revision_no), 0) AS rn FROM fact_revisions WHERE fact_id=?",
        (fact_id,),
    ).fetchone()["rn"]
    return _FactCursor(
        f["id"], f["entity_id"], f["object"], f["status"],
        f["version"], f["valid_from_chapter"], rn,
    )


def _object_of(prop: BibleChangeProposal) -> str:
    """从 new 派生 facts.object（NOT NULL）。优先显式 object，否则取语义值，最后整体 JSON 兜底。"""
    n = prop.new or {}
    return str(
        n.get("object")
        or n.get("to")
        or n.get("rank_name")
        or n.get("value")
        or json.dumps(n, ensure_ascii=False)
    )


def _insert_revision(
    conn,
    fact_id,
    revision_no,
    op,
    *,
    new_status,
    valid_from_chapter,
    reason,
    actor,
    old_object=None,
    new_object=None,
    old_status=None,
    policy_mode=None,
    cand=None,
) -> str:
    rev_id = new_id("rev")
    conn.execute(
        "INSERT INTO fact_revisions"
        "(id, fact_id, revision_no, op, old_object, new_object, old_status,"
        " new_status, valid_from_chapter, reason, evidence_refs, actor, policy_mode,"
        " source_candidate_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rev_id, fact_id, revision_no, op, old_object, new_object, old_status,
            new_status, valid_from_chapter, reason,
            getattr(cand, "evidence_refs", None),
            actor, policy_mode,
            getattr(cand, "candidate_id", None),
        ),
    )
    return rev_id


def _insert_fact(
    conn,
    fact_id,
    prop: BibleChangeProposal,
    *,
    current_revision_id: str,
    status: str,
    version: int,
) -> None:
    eid = None
    if prop.entity:
        try:
            eid = resolve_entity_id(prop.entity, conn)
        except ProjectionError:
            eid = None  # 软记忆/无实体 fact 允许 entity_id NULL
    n = prop.new or {}
    conn.execute(
        "INSERT INTO facts"
        "(id, entity_id, fact_type, subject, predicate, object, detail_json, status,"
        " valid_from_chapter, current_revision_id, version)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            fact_id, eid, prop.fact_type,
            n.get("subject") or prop.entity or prop.fact_type,
            n.get("predicate") or prop.fact_type,
            _object_of(prop),
            json.dumps(n, ensure_ascii=False),
            status, prop.valid_from_chapter, current_revision_id, version,
        ),
    )


def proposal_to_transition(
    prop: BibleChangeProposal, fact_id: str, conn
) -> StateTransition | None:
    """commit 时把 fact 提案反解为投影用 StateTransition。new 须含 'facet'（或 fact_type 可映射）。"""
    n = prop.new or {}
    facet = n.get("facet") or HARD_FACET.get(prop.fact_type)
    if facet is None:
        return None  # 纯叙事/world_rule/style fact，无 *_log 投影
    return StateTransition(
        entity_id=prop.entity or "",
        facet=facet,
        to_value=str(n.get("to") or n.get("rank_name") or n.get("object") or ""),
        at_chapter=prop.valid_from_chapter,
        kind=n.get("change_type") or n.get("kind"),
        payload={**n, "facet": facet},
    )


def project_to_world_state(
    fact_id: str, prop: BibleChangeProposal, conn
) -> None:
    """⑤ 唯一的 *_log 写入口。纯叙事 fact 直接 no-op。"""
    t = proposal_to_transition(prop, fact_id, conn)
    if t is not None:
        apply_state_transition(t, source_fact_id=fact_id, conn=conn)


def _insert_promotion_log(
    conn,
    *,
    candidate_id,
    fact_id,
    entity_id,
    decision,
    policy_mode,
    risk_tier,
    reason,
    actor,
    chapter=None,
    conflict_summary=None,
    old_value=None,
    new_value=None,
    reverts_log_id=None,
) -> str:
    plog_id = new_id("plog")
    conn.execute(
        "INSERT INTO promotion_log"
        "(id, candidate_id, fact_id, entity_id, decision, policy_mode, risk_tier,"
        " evidence_strength, chapter, conflict_summary, old_value, new_value,"
        " reason, actor, reverts_log_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            plog_id, candidate_id, fact_id, entity_id, decision, policy_mode, risk_tier,
            None, chapter, conflict_summary, old_value, new_value, reason, actor, reverts_log_id,
        ),
    )
    return plog_id


# ── 原子晋升入口（替换 §11.7）────────────────────────────────────────────────────

def commit_canon(cand, conn: sqlite3.Connection, *, policy_mode: str, actor: str) -> str:
    """原子晋升：① fact_revisions ② facts 游标 ③ promotion_log ④ 候选→promoted ⑤ 投影 *_log。
    policy_mode/actor 由调用方（apply_gate_routes）从 config/ctx 传入，非取自 cand。"""
    from ..world.projection import reproject_affected  # avoid circular at module level

    with conn:
        prop = BibleChangeProposal.model_validate_json(cand.proposal_json)
        reason = f"{prop.op}:{prop.fact_type}"
        entity_for_reproject = None
        fact_id: str = ""

        if prop.op == "add":
            fact_id = new_id("fact")
            # ② insert fact first (current_revision_id has no FK; use placeholder)
            _insert_fact(conn, fact_id, prop, current_revision_id="pending", status="canon", version=0)
            rev_id = _insert_revision(  # ① fact exists now → FK satisfied
                conn, fact_id, 1, "add",
                new_status="canon", valid_from_chapter=prop.valid_from_chapter,
                reason=reason, actor=actor, policy_mode=policy_mode,
                new_object=_object_of(prop), cand=cand,
            )
            conn.execute("UPDATE facts SET current_revision_id=? WHERE id=?", (rev_id, fact_id))
            project_to_world_state(fact_id, prop, conn)  # ⑤

        elif prop.op == "update":
            fact_id = cand.target_fact_id
            cur = _read_fact(conn, fact_id)
            new_obj = _object_of(prop)
            rev_id = _insert_revision(
                conn, fact_id, cur.revision_no + 1, "update",
                old_object=cur.object, new_object=new_obj,
                new_status="canon", valid_from_chapter=prop.valid_from_chapter,
                reason=reason, actor=actor, policy_mode=policy_mode, cand=cand,
            )  # ①
            n = conn.execute(  # ② 乐观锁
                "UPDATE facts SET current_revision_id=?, object=?, detail_json=?,"
                "  status='canon', valid_from_chapter=?, version=version+1,"
                "  updated_at=datetime('now')"
                " WHERE id=? AND version=?",
                (
                    rev_id, new_obj,
                    json.dumps(prop.new, ensure_ascii=False),
                    prop.valid_from_chapter, fact_id, cur.version,
                ),
            ).rowcount
            if n == 0:
                raise OptimisticLockError(fact_id)
            project_to_world_state(fact_id, prop, conn)  # ⑤ 追加新 *_log 行

        elif prop.op == "deprecate":
            fact_id = cand.target_fact_id
            cur = _read_fact(conn, fact_id)
            rev_id = _insert_revision(
                conn, fact_id, cur.revision_no + 1, "deprecate",
                old_object=cur.object, new_object=cur.object,
                new_status="canon", valid_from_chapter=prop.valid_from_chapter,
                reason=reason, actor=actor, policy_mode=policy_mode, cand=cand,
            )  # ①
            conn.execute(
                "UPDATE facts SET current_revision_id=?, valid_to_chapter=?,"
                " version=version+1, updated_at=datetime('now')"
                " WHERE id=? AND version=?",
                (rev_id, prop.valid_from_chapter, fact_id, cur.version),
            )
            entity_for_reproject = cur.entity_id  # 关区间后须重投影

        elif prop.op == "retcon":  # §03.7.1：标旧 retconned + append 新 fact
            old_id = cand.target_fact_id
            old = _read_fact(conn, old_id)
            _insert_revision(
                conn, old_id, old.revision_no + 1, "retcon",
                old_object=old.object, new_object=old.object,
                old_status=old.status, new_status="retconned",
                valid_from_chapter=prop.valid_from_chapter,
                reason=f"retcon←{old_id}",
                actor=actor, policy_mode=policy_mode, cand=cand,
            )  # ① 旧 fact 留痕
            conn.execute(
                "UPDATE facts SET status='retconned', version=version+1,"
                " updated_at=datetime('now') WHERE id=? AND version=?",
                (old_id, old.version),
            )
            fact_id = new_id("fact")
            _insert_fact(conn, fact_id, prop, current_revision_id="pending", status="canon", version=0)
            rev_id = _insert_revision(
                conn, fact_id, 1, "add",
                new_status="canon", valid_from_chapter=prop.valid_from_chapter,
                reason=f"retcon→{old_id}",
                actor=actor, policy_mode=policy_mode,
                new_object=_object_of(prop), cand=cand,
            )
            conn.execute("UPDATE facts SET current_revision_id=? WHERE id=?", (rev_id, fact_id))
            entity_for_reproject = old.entity_id  # 级联：按 canon 全量重放

        _insert_promotion_log(
            conn,
            candidate_id=cand.candidate_id,
            fact_id=fact_id,
            entity_id=cand.entity_id,
            decision="commit_canon",
            policy_mode=policy_mode,
            risk_tier=cand.risk_tier,
            reason=reason,
            actor=actor,
            chapter=cand.source_chapter,
        )  # ③
        conn.execute(  # ④
            "UPDATE fact_candidates SET status='promoted', committed_revision_id=?,"
            " decided_at=datetime('now') WHERE candidate_id=?",
            (rev_id, cand.candidate_id),
        )

        if entity_for_reproject is not None:
            reproject_affected(entity_for_reproject, conn)  # ⑤' 级联

    return fact_id
