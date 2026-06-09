"""PipelineManager：L1/L2/L3 记忆管线触发器（§06.2）。

MVP1 同步实现，L2/L3 为存根（预留接口）。
L1：每章提取实体+关键词，写 fact_candidates。
L2：卷边界或每 3-5 章摘要（存根）。
L3：事件驱动（world_rule/new_entity/foreshadowing）（存根）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from ..ids import new_id


@dataclass
class PipelineResult:
    chapter: int
    candidates_added: int = 0
    l1_ok: bool = True
    l2_triggered: bool = False
    l3_triggered: bool = False
    errors: list[str] = field(default_factory=list)


class PipelineManager:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── L1: 每章提取（MVP1：规则基础提取，不调用 LLM）──────────────────────────

    def run_l1(self, chapter: int, draft_text: str, project_id: str = "default") -> PipelineResult:
        """从草稿文本提取实体引用，生成 fact_candidates（proposed）。"""
        result = PipelineResult(chapter=chapter)
        try:
            # 提取章内提到的实体名
            entity_hits = self._find_entity_mentions(draft_text)
            # 写草稿到 drafts 表（如果存在）
            self._upsert_draft(chapter, draft_text, project_id)
            # 为每个新实体提及生成候选（保守起见只生成 add 类型的候选）
            added = self._generate_mention_candidates(chapter, entity_hits, project_id)
            result.candidates_added = added
        except Exception as e:
            result.l1_ok = False
            result.errors.append(str(e))
        return result

    # ── L2: 卷摘要（存根）──────────────────────────────────────────────────────

    def should_run_l2(self, chapter: int, volume_size: int = 5) -> bool:
        return chapter > 0 and chapter % volume_size == 0

    def run_l2(self, chapter: int) -> PipelineResult:
        """L2 卷摘要——MVP1 存根，实际摘要由 LLM 完成（未实现）。"""
        return PipelineResult(chapter=chapter, l2_triggered=True)

    # ── L3: 事件驱动（存根）────────────────────────────────────────────────────

    def run_l3_if_needed(self, chapter: int, events: list[str]) -> PipelineResult:
        """L3 事件驱动——MVP1 存根。"""
        triggered = bool(events)
        return PipelineResult(chapter=chapter, l3_triggered=triggered)

    # ── 私有 ───────────────────────────────────────────────────────────────────

    def _find_entity_mentions(self, text: str) -> list[str]:
        """从 DB 拉取实体名列表，对草稿做简单字符串匹配，返回命中 entity_id 列表。"""
        entities = self._conn.execute(
            "SELECT id, canonical_name FROM entities LIMIT 200"
        ).fetchall()
        hits: list[str] = []
        for e in entities:
            name = e["canonical_name"] or ""
            if name and name in text:
                hits.append(e["id"])
        return list(set(hits))

    def _upsert_draft(self, chapter: int, text: str, project_id: str) -> None:
        try:
            self._conn.execute(
                "INSERT INTO drafts(id, project_id, chapter, content, status)"
                " VALUES(?,?,?,?,'draft')"
                " ON CONFLICT(project_id, chapter) DO UPDATE SET"
                "   content=excluded.content, updated_at=datetime('now')",
                (new_id("draft"), project_id, chapter, text),
            )
        except Exception:
            pass  # drafts 表不存在时静默

    def _generate_mention_candidates(
        self, chapter: int, entity_ids: list[str], project_id: str
    ) -> int:
        """为首次出现的实体生成 appearance 候选（保守 low risk）。"""
        added = 0
        for eid in entity_ids:
            # 检查是否已有同 entity + chapter 的候选
            exists = self._conn.execute(
                "SELECT 1 FROM fact_candidates WHERE entity_id=? AND source_chapter=?",
                (eid, chapter),
            ).fetchone()
            if exists:
                continue
            proposal = json.dumps({
                "op": "add",
                "fact_type": "appearance",
                "entity": eid,
                "new": {"subject": eid, "predicate": "appears_in", "object": f"chapter_{chapter}"},
                "valid_from_chapter": chapter,
            }, ensure_ascii=False)
            try:
                self._conn.execute(
                    "INSERT INTO fact_candidates"
                    "(candidate_id, op, entity_id, fact_type, proposal_json, status, risk_tier, source_chapter)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (new_id("cand"), "add", eid, "appearance", proposal, "proposed", "low", chapter),
                )
                added += 1
            except Exception:
                pass
        return added
