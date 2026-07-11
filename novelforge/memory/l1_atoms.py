"""L1 可追溯事件原子。

L1 不是 canon，也不直接注入生成 prompt。它保存“选中正文产生了哪些结构化
提案、证据落在哪些 L0 行、随后进入了哪个 candidate”，用于审计、重放和健康
检查。正常生成复用 ChapterDraftSkill 已有 proposals，因此不增加 LLM 调用。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Iterable, Optional

from ..contracts import normalize_proposal_fact_type
from ..db.l0 import load_draft_text_by_id


def _canonical_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atom_text(proposal: dict) -> str:
    payload = proposal.get("new") if isinstance(proposal.get("new"), dict) else {}
    parts = [payload.get("subject"), payload.get("predicate"), payload.get("object")]
    text = " ".join(str(p).strip() for p in parts if p is not None and str(p).strip())
    return text or _canonical_json(proposal)


def _evidence_tokens(proposal: dict) -> list[tuple[str, int]]:
    payload = proposal.get("new") if isinstance(proposal.get("new"), dict) else {}
    raw: list[tuple[object, int]] = [
        (payload.get("object"), 4),
        (payload.get("predicate"), 2),
        (payload.get("subject"), 1),
        (proposal.get("entity"), 1),
        (proposal.get("target_fact_id"), 1),
    ]
    weighted: dict[str, int] = {}
    for value, weight in raw:
        if isinstance(value, (str, int, float)):
            token = str(value).strip()
            if len(token) >= 2:
                weighted[token] = max(weighted.get(token, 0), weight)
    return sorted(weighted.items(), key=lambda item: (item[1], len(item[0])), reverse=True)


def find_evidence_span(draft_text: str, proposal: dict) -> tuple[Optional[int], Optional[int], str]:
    """在 L0 中选择命中提案关键 token 最多的一行作为可核验锚点。"""
    lines = (draft_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    tokens = _evidence_tokens(proposal)
    best: tuple[int, int, str] | None = None
    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        score = sum(weight for token, weight in tokens if token in stripped)
        if score and (best is None or score > best[0]):
            best = (score, line_no, stripped)
    if best is None:
        return None, None, ""
    return best[1], best[1], best[2][:500]


def persist_proposal_atoms(
    conn: sqlite3.Connection,
    *,
    chapter: int,
    draft_id: str | None,
    draft_text: str,
    proposals: Iterable[dict],
    candidate_ids: Iterable[str | None] | None = None,
    cold_start_flags: Iterable[bool] | None = None,
    extracted_by: str = "chapter_draft_proposal_v1",
    cold_start: bool = False,
    commit: bool = True,
) -> dict[str, object]:
    """幂等保存 proposals 为 L1 atoms，并标记对应 draft 已完成 L1。"""
    proposal_list = [normalize_proposal_fact_type(dict(p)) for p in proposals]
    ids = list(candidate_ids or [])
    source_flags = list(cold_start_flags or [])
    atoms = anchored = unanchored = 0
    for index, proposal in enumerate(proposal_list):
        payload_json = _canonical_json(proposal)
        content_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        identity = f"{draft_id or f'chapter:{chapter}'}:{content_hash}"
        atom_id = "atom_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:26]
        start, end, anchor = find_evidence_span(draft_text, proposal)
        status = "active" if start is not None else "unanchored"
        candidate_id = ids[index] if index < len(ids) else None
        conn.execute(
            "INSERT INTO l1_atoms("
            " id, chapter, draft_id, atom_text, anchor, extracted_by, candidate_id,"
            " atom_type, payload_json, source_line_start, source_line_end, content_hash,"
            " status, cold_start)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            "   candidate_id=COALESCE(excluded.candidate_id,l1_atoms.candidate_id),"
            "   anchor=excluded.anchor, source_line_start=excluded.source_line_start,"
            "   source_line_end=excluded.source_line_end, status=excluded.status,"
            "   payload_json=excluded.payload_json, extracted_by=excluded.extracted_by",
            (
                atom_id, chapter, draft_id, _atom_text(proposal), anchor or None,
                extracted_by, candidate_id, proposal.get("fact_type"), payload_json,
                start, end, content_hash, status,
                1 if (source_flags[index] if index < len(source_flags) else cold_start) else 0,
            ),
        )
        atoms += 1
        if status == "active":
            anchored += 1
        else:
            unanchored += 1

    if draft_id is not None:
        try:
            conn.execute(
                "UPDATE draft_index SET l1_status='completed',"
                " l1_extracted_at=datetime('now') WHERE id=?",
                (draft_id,),
            )
        except sqlite3.Error:
            pass
    if commit:
        conn.commit()
    return {"atoms": atoms, "anchored": anchored, "unanchored": unanchored}


def rebuild_l1_atoms(conn: sqlite3.Connection, *, force: bool = False) -> dict[str, int]:
    """用历史 fact_candidates 为每章最新 L0 做 L1 最佳努力回填。"""
    drafts = conn.execute(
        "SELECT di.id, di.chapter, di.l1_status FROM draft_index di"
        " JOIN (SELECT chapter, MAX(revision_round) AS revision_round"
        "       FROM draft_index GROUP BY chapter) latest"
        "   ON latest.chapter=di.chapter AND latest.revision_round=di.revision_round"
        " ORDER BY di.chapter"
    ).fetchall()
    processed = atoms = anchored = unanchored = missing_files = failed = ambiguous = 0
    for draft in drafts:
        if not force and draft["l1_status"] == "completed":
            continue
        text = load_draft_text_by_id(conn, draft["id"])
        if text is None:
            missing_files += 1
            try:
                conn.execute(
                    "UPDATE draft_index SET l1_status='failed' WHERE id=?", (draft["id"],)
                )
            except sqlite3.Error:
                pass
            conn.commit()
            continue
        revision_count = conn.execute(
            "SELECT COUNT(*) AS n FROM draft_index WHERE chapter=?",
            (draft["chapter"],),
        ).fetchone()["n"]
        rows = conn.execute(
            "SELECT candidate_id, proposal_json, source_skill FROM fact_candidates"
            " WHERE source_draft_id=?"
            "    OR (source_draft_id IS NULL AND source_chapter=? AND ?=1)"
            " ORDER BY created_at, candidate_id",
            (draft["id"], draft["chapter"], revision_count),
        ).fetchall()
        if revision_count > 1:
            legacy_count = conn.execute(
                "SELECT COUNT(*) AS n FROM fact_candidates"
                " WHERE source_chapter=? AND source_draft_id IS NULL",
                (draft["chapter"],),
            ).fetchone()["n"]
            ambiguous += int(legacy_count)
        proposals: list[dict] = []
        candidate_ids: list[str] = []
        cold_start_flags: list[bool] = []
        for row in rows:
            try:
                proposals.append(json.loads(row["proposal_json"]))
                candidate_ids.append(row["candidate_id"])
                cold_start_flags.append(row["source_skill"] == "cold_extract")
            except (TypeError, json.JSONDecodeError):
                continue
        try:
            conn.execute("SAVEPOINT l1_rebuild_chapter")
            if force:
                # 仅替换当前 draft 的派生原子；历史 revision 的审计证据保留。
                conn.execute(
                    "DELETE FROM l1_atoms WHERE draft_id=?"
                    " AND NOT (cold_start=1 AND atom_type='cold_start_source')",
                    (draft["id"],),
                )
            stats = persist_proposal_atoms(
                conn,
                chapter=draft["chapter"],
                draft_id=draft["id"],
                draft_text=text,
                proposals=proposals,
                candidate_ids=candidate_ids,
                cold_start_flags=cold_start_flags,
                extracted_by="backfill_fact_candidate_v2",
                commit=False,
            )
            conn.execute("RELEASE SAVEPOINT l1_rebuild_chapter")
            conn.commit()
            processed += 1
            atoms += int(stats["atoms"])
            anchored += int(stats["anchored"])
            unanchored += int(stats["unanchored"])
        except Exception:
            try:
                conn.execute("ROLLBACK TO SAVEPOINT l1_rebuild_chapter")
                conn.execute("RELEASE SAVEPOINT l1_rebuild_chapter")
            except sqlite3.Error:
                conn.rollback()
            try:
                conn.execute(
                    "UPDATE draft_index SET l1_status='failed' WHERE id=?",
                    (draft["id"],),
                )
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
            failed += 1
    return {
        "processed_chapters": processed,
        "atoms": atoms,
        "anchored": anchored,
        "unanchored": unanchored,
        "missing_files": missing_files,
        "failed_chapters": failed,
        "ambiguous_legacy_candidates": ambiguous,
    }
