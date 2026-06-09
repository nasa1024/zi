"""append-only 回滚（§03.7.2 / §9.1.3）。

revert_fact(fact_id, conn, *, actor, reason) → 在 fact_revisions 追加 op="revert" 条目，
facts 状态回到前一有效版本；promotion_log 追加回滚记录。物理行永不删除。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from ..ids import new_id
from .commit import _insert_revision, _insert_promotion_log


def revert_fact(
    fact_id: str,
    conn: sqlite3.Connection,
    *,
    actor: str,
    reason: str,
    revert_to_revision_id: Optional[str] = None,
) -> dict:
    """把 fact 回退到前一个修订（或指定修订）。返回结果字典。

    策略：
    - 取 fact_revisions 中 fact_id 最新的一条（若指定 revert_to_revision_id 则取该条）
    - 把 facts.object/status/valid_from_chapter 回写为那一版的 old_object/old_status
    - 在 fact_revisions 追加新的 op="revert" 条目
    - 在 promotion_log 追加 decision="revert"
    """
    fact = conn.execute(
        "SELECT id, object, status, version, valid_from_chapter, entity_id FROM facts WHERE id=?",
        (fact_id,),
    ).fetchone()
    if fact is None:
        raise ValueError(f"facts 无此行: {fact_id}")

    # 取目标修订（前一版或指定版）
    if revert_to_revision_id:
        target_rev = conn.execute(
            "SELECT * FROM fact_revisions WHERE id=? AND fact_id=?",
            (revert_to_revision_id, fact_id),
        ).fetchone()
        if target_rev is None:
            raise ValueError(f"fact_revisions 无此修订: {revert_to_revision_id}")
    else:
        # 取最新修订的前一条（倒数第二）
        revs = conn.execute(
            "SELECT * FROM fact_revisions WHERE fact_id=? ORDER BY revision_no DESC LIMIT 2",
            (fact_id,),
        ).fetchall()
        if len(revs) < 2:
            raise ValueError(f"fact {fact_id} 没有可回退的前一版本（只有 {len(revs)} 条修订）")
        target_rev = revs[1]

    # 回退值
    old_object = target_rev["old_object"] or target_rev["new_object"] or fact["object"]
    old_status = target_rev["old_status"] or "canon"
    revert_from_chapter = fact["valid_from_chapter"]
    revert_to_chapter = target_rev["valid_from_chapter"]

    # 最新修订号
    max_rn = conn.execute(
        "SELECT COALESCE(MAX(revision_no), 0) AS rn FROM fact_revisions WHERE fact_id=?",
        (fact_id,),
    ).fetchone()["rn"]

    # ① 追加 revert 修订
    rev_id = _insert_revision(
        conn, fact_id, max_rn + 1, "revert",
        new_status=old_status,
        valid_from_chapter=revert_to_chapter,
        reason=reason,
        actor=actor,
        old_object=fact["object"],
        new_object=old_object,
        old_status=fact["status"],
        policy_mode="human_gate",
    )

    # ② 更新 facts 行（乐观锁 version）
    conn.execute(
        "UPDATE facts SET object=?, status=?, valid_from_chapter=?,"
        "  current_revision_id=?, version=version+1, updated_at=datetime('now')"
        " WHERE id=? AND version=?",
        (old_object, old_status, revert_to_chapter, rev_id, fact_id, fact["version"]),
    )

    # ③ promotion_log append
    # 找该 fact 最近一次的 promotion_log 条目，作为被撤销的记录（self-ref FK）
    prev_plog = conn.execute(
        "SELECT id FROM promotion_log WHERE fact_id=? ORDER BY created_at DESC LIMIT 1",
        (fact_id,),
    ).fetchone()
    reverts_log_id = prev_plog["id"] if prev_plog else None

    plog_id = _insert_promotion_log(
        conn,
        candidate_id=None,
        fact_id=fact_id,
        entity_id=fact["entity_id"],
        decision="revert",
        policy_mode="human_gate",
        risk_tier="low",
        reason=reason,
        actor=actor,
        old_value=fact["object"],
        new_value=old_object,
        reverts_log_id=reverts_log_id,
    )

    return {
        "fact_id": fact_id,
        "reverted_to": target_rev["id"],
        "promotion_log_id": plog_id,
    }
