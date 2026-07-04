"""Branch merge（POST-v15 P1#3）：支线剧情并入主线的治理提案。

分支上的 canon facts 不会自动污染主线（build_branch_filter 只在分支读路径
可见）。当作者决定把支线 B 并入主线时：

  preview_branch_merge()  列出待并入的分支 facts + 与主线的确定性冲突
  merge_branch_to_mainline()  逐条降级为 BibleChangeProposal 写入
      fact_candidates（staging）并入 review_queue —— 走主线 governance
      管线（human_gate 人审 / batch_approve），绝不直接落 canon。

冲突判定（确定性，无 LLM）：主线上存在同 subject+predicate、object 不同、
生效区间未关闭的 canon fact → 该条提案 risk_tier 升 high、op 转 'retcon'
（并入即改写主线既定事实，必须人审）。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from ..ids import new_id


def _branch_row(conn: sqlite3.Connection, branch_id: str):
    return conn.execute(
        "SELECT id, branch_name, fork_chapter, base_branch_id, status"
        " FROM branches WHERE id=?", (branch_id,),
    ).fetchone()


def _branch_canon_facts(conn: sqlite3.Connection, branch_id: str) -> list:
    return conn.execute(
        "SELECT * FROM facts"
        " WHERE branch_id=? AND status='canon' AND valid_to_chapter IS NULL"
        " ORDER BY valid_from_chapter, created_at",
        (branch_id,),
    ).fetchall()


def _find_mainline_conflict(conn: sqlite3.Connection, fact_row) -> Optional[dict]:
    """主线（branch_id IS NULL）同 subject+predicate、不同 object 的开放 canon fact。"""
    row = conn.execute(
        "SELECT id, object, valid_from_chapter FROM facts"
        " WHERE branch_id IS NULL AND status='canon' AND valid_to_chapter IS NULL"
        "   AND subject=? AND predicate=? AND object<>?"
        " ORDER BY valid_from_chapter DESC LIMIT 1",
        (fact_row["subject"], fact_row["predicate"], fact_row["object"]),
    ).fetchone()
    if row is None:
        return None
    return {"fact_id": row["id"], "object": row["object"],
            "valid_from_chapter": row["valid_from_chapter"]}


def _fact_to_proposal(fact_row, branch_id: str, conflict: Optional[dict]) -> dict:
    try:
        new = json.loads(fact_row["detail_json"]) if fact_row["detail_json"] else {}
    except (json.JSONDecodeError, TypeError):
        new = {}
    new.setdefault("subject", fact_row["subject"])
    new.setdefault("predicate", fact_row["predicate"])
    new.setdefault("object", fact_row["object"])
    prop = {
        "op": "retcon" if conflict else "add",
        "fact_type": fact_row["fact_type"],
        "new": new,
        "valid_from_chapter": fact_row["valid_from_chapter"],
        "source": "branch_merge",
        "source_branch_id": branch_id,
        "source_branch_fact_id": fact_row["id"],
    }
    if fact_row["entity_id"]:
        prop["entity"] = fact_row["entity_id"]
    if conflict:
        prop["target_fact_id"] = conflict["fact_id"]
        prop["conflicts_with"] = conflict
    return prop


def preview_branch_merge(conn: sqlite3.Connection, branch_id: str) -> dict:
    """列出分支待并入的 facts 与主线冲突，不做任何写入。"""
    br = _branch_row(conn, branch_id)
    if br is None:
        return {"found": False, "branch_id": branch_id}
    facts = _branch_canon_facts(conn, branch_id)
    items: list[dict] = []
    conflict_count = 0
    for f in facts:
        conflict = _find_mainline_conflict(conn, f)
        if conflict:
            conflict_count += 1
        items.append({
            "fact_id": f["id"],
            "fact_type": f["fact_type"],
            "subject": f["subject"],
            "predicate": f["predicate"],
            "object": f["object"],
            "valid_from_chapter": f["valid_from_chapter"],
            "risk_tier": f["risk_tier"],
            "conflict": conflict,
        })
    return {
        "found": True,
        "branch_id": branch_id,
        "branch_name": br["branch_name"],
        "fork_chapter": br["fork_chapter"],
        "branch_status": br["status"],
        "fact_count": len(items),
        "conflict_count": conflict_count,
        "facts": items,
    }


def merge_branch_to_mainline(
    conn: sqlite3.Connection, branch_id: str, *, actor: str = "branch_merge",
) -> dict:
    """把分支 canon facts 转为主线 staging 提案 + 入审队列，分支标记 merged。

    冲突条目 op='retcon'、risk_tier='high'；无冲突条目保留原 risk_tier。
    全部候选进 review_queue（分支并线是结构性变更，一律人审兜底）。
    调用方负责最终 conn.commit()（本函数内已提交——保持端点语义简单）。
    """
    br = _branch_row(conn, branch_id)
    if br is None:
        return {"merged": False, "reason": "branch_not_found"}
    if br["status"] == "merged":
        return {"merged": False, "reason": "already_merged"}

    facts = _branch_canon_facts(conn, branch_id)
    candidate_ids: list[str] = []
    conflict_count = 0
    for f in facts:
        conflict = _find_mainline_conflict(conn, f)
        prop = _fact_to_proposal(f, branch_id, conflict)
        risk = "high" if conflict else (f["risk_tier"] or "low")
        if conflict:
            conflict_count += 1
        cid = new_id("cand")
        conn.execute(
            "INSERT INTO fact_candidates"
            "(candidate_id, op, entity_id, fact_type, proposal_json,"
            " status, risk_tier, source_chapter, target_fact_id)"
            " VALUES(?,?,?,?,?,'pending_review',?,?,?)",
            (cid, prop["op"], f["entity_id"], f["fact_type"],
             json.dumps(prop, ensure_ascii=False), risk,
             f["valid_from_chapter"], prop.get("target_fact_id")),
        )
        reason = (f"分支并线 {br['branch_name']}"
                  + ("（与主线既定事实冲突，retcon）" if conflict else ""))
        conn.execute(
            "INSERT INTO review_queue(id, candidate_id, priority, risk_tier, reason, status)"
            " VALUES(?,?,?,?,?,'pending')",
            (new_id("rq"), cid, 1 if conflict else 5, risk, reason),
        )
        candidate_ids.append(cid)

    conn.execute("UPDATE branches SET status='merged' WHERE id=?", (branch_id,))
    conn.commit()
    return {
        "merged": True,
        "branch_id": branch_id,
        "branch_name": br["branch_name"],
        "candidate_ids": candidate_ids,
        "fact_count": len(candidate_ids),
        "conflict_count": conflict_count,
    }
