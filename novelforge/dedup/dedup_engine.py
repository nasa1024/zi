"""近邻去重引擎（§11.5）。

流程：FTS 近邻 → 阈值判定 → LLM 仲裁（Haiku/Fast）
输出：DedupVerdict { store | merge | conflict }
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Optional

from ..contracts import FactCandidate


@dataclass
class DedupVerdict:
    action: str           # "store" | "merge" | "conflict"
    target_id: Optional[str] = None   # merge 时：被合并进的 candidate_id 或 fact_id
    reason: str = ""


class DeduplicationEngine:
    """
    cos_threshold: FTS BM25 分数差阈值（score/top1_score < gap_min 认为相似）
    arbiter_model: LLM 仲裁用哪档模型
    """

    def __init__(
        self,
        bm25_gap_min: float = 2.0,
        llm_gateway=None,
    ) -> None:
        self._gap = bm25_gap_min
        self._gw = llm_gateway

    def check(self, cand: FactCandidate, conn: sqlite3.Connection) -> DedupVerdict:
        """对一个 proposed 候选做去重检查。"""
        # 1. 提取关键词
        query = self._candidate_query(cand)
        if not query:
            return DedupVerdict(action="store", reason="no_query_text")

        # 2. FTS 近邻查 facts_fts（canon 已提交的 facts）
        hit_fact = self._fts_facts(query, conn)
        if hit_fact:
            verdict = self._arbiter(cand, hit_fact, "fact", conn)
            if verdict.action != "store":
                return verdict

        # 3. FTS 近邻查 fact_candidates（同章其他 proposed 候选）
        hit_cand = self._fts_candidates(query, cand.candidate_id, conn)
        if hit_cand:
            verdict = self._arbiter(cand, hit_cand, "candidate", conn)
            if verdict.action != "store":
                return verdict

        return DedupVerdict(action="store", reason="no_near_neighbor")

    # ── FTS 查询 ────────────────────────────────────────────────────────────

    def _candidate_query(self, cand: FactCandidate) -> str:
        try:
            prop = json.loads(cand.proposal_json)
            n = prop.get("new") or {}
            parts = [
                str(n.get("subject") or ""),
                str(n.get("predicate") or prop.get("fact_type") or ""),
                str(n.get("object") or n.get("rank_name") or ""),
            ]
            return " ".join(p for p in parts if p).strip()
        except Exception:
            return ""

    def _fts_facts(self, query: str, conn) -> Optional[dict]:
        try:
            rows = conn.execute(
                "SELECT fact_id, bm25(facts_fts) AS score"
                " FROM facts_fts WHERE facts_fts MATCH ? ORDER BY score LIMIT 2",
                (_fts_escape(query),),
            ).fetchall()
            if not rows:
                return None
            top = dict(rows[0])
            if len(rows) > 1:
                gap = abs(top["score"] / rows[1]["score"]) if rows[1]["score"] else 999
                if gap < self._gap:
                    return None  # 分数太接近，不够确定
            return top
        except Exception:
            return None

    def _fts_candidates(self, query: str, exclude_id: str, conn) -> Optional[dict]:
        """在 fact_candidates 的 proposal_json 做全文检索（简化版：字符串包含）。"""
        try:
            rows = conn.execute(
                "SELECT candidate_id FROM fact_candidates"
                " WHERE status='proposed' AND candidate_id <> ?"
                "   AND proposal_json LIKE ?",
                (exclude_id, f"%{query[:20]}%"),
            ).fetchall()
            if rows:
                return {"candidate_id": rows[0]["candidate_id"]}
        except Exception:
            pass
        return None

    # ── LLM 仲裁 ────────────────────────────────────────────────────────────

    _ARBITER_SYSTEM = """\
你是 NovelForge 的去重仲裁员。判断两条候选事实是否重复。
输出严格 JSON：{"action": "store"|"merge"|"conflict", "reason": "一句话"}
- store：两者独立，都保留
- merge：新候选是旧条目的重复或补充 → 合并 evidence_refs，旧的 superseded
- conflict：两者矛盾 → 标记 conflict_flags，交人工处理
只输出 JSON，不要其他文字。"""

    def _arbiter(
        self, cand: FactCandidate, hit: dict, hit_type: str, conn
    ) -> DedupVerdict:
        if self._gw is None:
            # 无 LLM 时保守 store
            return DedupVerdict(action="store", reason="no_llm_arbiter")

        # 取命中条目的摘要
        hit_summary = self._hit_summary(hit, hit_type, conn)
        cand_summary = self._candidate_query(cand)

        try:
            from ..control_plane.llm.provider import Message
            from ..control_plane.llm.tiers import ModelTier
            resp = self._gw.generate(
                ModelTier.FAST,
                [Message(role="user", content=(
                    f"候选A（新）：{cand_summary}\n"
                    f"候选B（已有/{hit_type}）：{hit_summary}\n"
                    "请判断：store / merge / conflict？"
                ))],
                system=self._ARBITER_SYSTEM,
                max_tokens=128,
            )
            result = json.loads(resp.text.strip())
            action = result.get("action", "store")
            reason = result.get("reason", "")
            target = hit.get("fact_id") or hit.get("candidate_id")
            return DedupVerdict(action=action, target_id=target, reason=reason)
        except Exception as e:
            return DedupVerdict(action="store", reason=f"arbiter_error:{e}")

    def _hit_summary(self, hit: dict, hit_type: str, conn) -> str:
        try:
            if hit_type == "fact":
                row = conn.execute(
                    "SELECT subject, predicate, object FROM facts WHERE id=?",
                    (hit["fact_id"],),
                ).fetchone()
                if row:
                    return f"{row['subject']} {row['predicate']} {row['object']}"
            else:
                row = conn.execute(
                    "SELECT proposal_json FROM fact_candidates WHERE candidate_id=?",
                    (hit["candidate_id"],),
                ).fetchone()
                if row:
                    p = json.loads(row["proposal_json"])
                    n = p.get("new") or {}
                    return f"{n.get('subject','')} {n.get('predicate','')} {n.get('object','')}"
        except Exception:
            pass
        return str(hit)


def _fts_escape(query: str) -> str:
    """简单转义 FTS5 特殊字符。"""
    for ch in ('"', "'", "*", "^", "(", ")"):
        query = query.replace(ch, " ")
    return query.strip() or '""'
