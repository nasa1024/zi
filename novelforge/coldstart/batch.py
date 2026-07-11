"""冷启动批量反向抽取管线（POST-v15 P1#4）。

单章版 ColdExtractSkill 只能"一章文本 → staging 提案"。本模块补齐存量迁移
的三块缺口：

1. split_manuscript()：整部已写好的正文（几十万字）按「第N章/回」标题自动
   切章；无标题时按固定字数分块兜底。
2. dedup_proposals()：冷启动会抽出大量重复/近似设定——先做规范化哈希精确
   去重，再在同 (entity, fact_type) 簇内做字符 bigram Jaccard 近邻去噪；
   与既有 canon facts 逐字重复的直接丢弃。
3. tier_route_proposals()：按 risk_tier 分层晋升——低风险可由程序直接
   commit_canon（auto_promote_low_risk=True 时），中/高风险统一进
   review_queue 批量审批队列，永不自动 canon。

ColdStartJobManager：后台线程跑整本导入（LLM 抽取按章并发，DB 写入在
job 线程串行），前端轮询 job 状态。与 AutopilotManager 同款单例模式。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Optional

from ..ids import new_id

# ── 1. 切章 ───────────────────────────────────────────────────────────────────

_CHAPTER_HEAD = re.compile(
    r"^\s*(?:第\s*([0-9零一二三四五六七八九十百千两]+)\s*[章回节卷]|Chapter\s+(\d+))"
    r"[^\n]*$",
    re.MULTILINE,
)

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(s: str) -> Optional[int]:
    s = s.strip()
    if s.isdigit():
        return int(s)
    # 简易中文数字（支持到千位，冷启动章号够用）
    total, section = 0, 0
    units = {"十": 10, "百": 100, "千": 1000}
    num = 0
    for ch in s:
        if ch in _CN_NUM:
            num = _CN_NUM[ch]
        elif ch in units:
            section += (num or 1) * units[ch]
            num = 0
        else:
            return None
    total = section + num
    return total or None


def split_manuscript(
    text: str, *, fallback_chunk_chars: int = 3000, max_chapters: int = 500,
) -> list[dict]:
    """整本正文 → [{chapter_no, title, text}]。

    优先按「第N章/回/节」标题切；无标题时按 fallback_chunk_chars 均块兜底
    （章号从 1 递增）。章号解析失败或重复时按出现顺序重编号。
    """
    text = (text or "").strip()
    if not text:
        return []

    heads = list(_CHAPTER_HEAD.finditer(text))
    chapters: list[dict] = []
    if len(heads) >= 2:
        for i, m in enumerate(heads[:max_chapters]):
            start = m.end()
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            body = text[start:end].strip()
            if not body:
                continue
            no = _cn_to_int(m.group(1) or m.group(2) or "")
            chapters.append({
                "chapter_no": no,
                "title": m.group(0).strip(),
                "text": body,
            })
        # 章号缺失/重复 → 按顺序重编号（保持相对顺序稳定）
        seen: set[int] = set()
        next_no = 1
        for ch in chapters:
            no = ch["chapter_no"]
            if not isinstance(no, int) or no <= 0 or no in seen:
                while next_no in seen:
                    next_no += 1
                ch["chapter_no"] = next_no
            seen.add(ch["chapter_no"])
            next_no = max(next_no, ch["chapter_no"] + 1)
        return chapters

    # 兜底：固定字数分块
    chunk = max(500, fallback_chunk_chars)
    return [
        {"chapter_no": i + 1, "title": f"自动分块 {i + 1}",
         "text": text[i * chunk:(i + 1) * chunk]}
        for i in range((len(text) + chunk - 1) // chunk)[:max_chapters]
        if text[i * chunk:(i + 1) * chunk].strip()
    ]


# ── 2. 去噪（哈希精确去重 + 近邻聚类）─────────────────────────────────────────

def _prop_key(p: dict) -> tuple:
    new = p.get("new") or {}
    return (
        str(p.get("fact_type") or "").strip(),
        str(p.get("entity") or new.get("subject") or "").strip(),
        str(new.get("predicate") or "").strip(),
        str(new.get("object") or "").strip(),
    )


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s} if s else set()


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedup_proposals(
    proposals: list[dict], conn: Optional[sqlite3.Connection] = None,
    *, near_threshold: float = 0.6,
) -> tuple[list[dict], int]:
    """三段式去噪：canon 已有 → 丢；规范化键重复 → 丢；同簇近邻 → 丢。

    Returns (kept, dropped_count)。near_threshold 为同 (entity, fact_type)
    簇内 "predicate object" 字符 bigram 的 Jaccard 上限，超过视为近似重复。
    """
    kept: list[dict] = []
    dropped = 0
    seen_keys: set[tuple] = set()
    # 簇: (entity, fact_type) → [bigram sets]
    clusters: dict[tuple, list[set]] = {}

    for p in proposals:
        key = _prop_key(p)
        # a) 规范化键精确去重（跨章的同一条设定只留最早一条）
        if key in seen_keys:
            dropped += 1
            continue
        # b) 与既有 canon facts 逐字重复 → 丢弃（存量迁移分批跑时防重灌）
        if conn is not None and key[1] and key[2] and key[3]:
            try:
                row = conn.execute(
                    "SELECT 1 FROM facts WHERE status='canon'"
                    "  AND subject=? AND predicate=? AND object=? LIMIT 1",
                    (key[1], key[2], key[3]),
                ).fetchone()
            except Exception:
                row = None
            if row is not None:
                dropped += 1
                continue
        # c) 近邻去噪：同 (entity, fact_type) 簇内 predicate+object 高度相似
        ckey = (key[1], key[0])
        grams = _bigrams(key[2] + key[3])
        if any(_jaccard(grams, g) >= near_threshold for g in clusters.get(ckey, [])):
            dropped += 1
            continue

        seen_keys.add(key)
        clusters.setdefault(ckey, []).append(grams)
        kept.append(p)
    return kept, dropped


# ── 3. 分层晋升路由 ───────────────────────────────────────────────────────────

def tier_route_proposals(
    conn: sqlite3.Connection,
    proposals: list[dict],
    *,
    auto_promote_low_risk: bool = False,
    actor: str = "cold_start",
    source_draft_ids: Optional[dict[int, str]] = None,
) -> dict:
    """按 risk_tier 分层落库：

    - low + auto_promote_low_risk → 直接 commit_canon（失败回落 staging）
    - low（不自动晋升）→ fact_candidates 'proposed'（暂存区，可批量批准）
    - medium/high → fact_candidates 'pending_review' + review_queue
      （专门的冷启动批量审批队列，reason 标注来源章）

    Returns {candidate_ids, auto_promoted, staged_low, queued_review, errors}
    """
    from ..contracts import FactCandidate, normalize_proposal_fact_type
    from ..db.write import with_retry
    from ..governance.commit import commit_canon

    out = {"candidate_ids": [], "auto_promoted": [], "staged_low": [],
           "queued_review": [], "errors": [], "candidate_links": []}
    for p in proposals:
        p = normalize_proposal_fact_type(dict(p))
        p.setdefault("op", "add")
        p.setdefault("new", {})
        risk = p.get("risk_tier") or "low"
        if risk not in ("low", "medium", "high"):
            risk = "low"
        chapter = int(p.get("valid_from_chapter") or 0)
        source_draft_id = (source_draft_ids or {}).get(chapter)
        prop_json = json.dumps(
            {**p, "source": "cold_start"}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
        cid = (
            "cand_" + hashlib.sha256(
                f"{source_draft_id}:{prop_json}".encode("utf-8")
            ).hexdigest()[:26]
            if source_draft_id else new_id("cand")
        )
        status = "pending_review" if risk != "low" else "proposed"
        inserted = conn.execute(
            "INSERT OR IGNORE INTO fact_candidates"
            "(candidate_id, op, entity_id, fact_type, proposal_json,"
            " status, risk_tier, source_chapter, source_skill, source_draft_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (cid, p["op"], None, p.get("fact_type", "misc"), prop_json,
             status, risk, chapter, "cold_extract", source_draft_id),
        ).rowcount
        out["candidate_ids"].append(cid)
        out["candidate_links"].append({
            "candidate_id": cid, "chapter": chapter,
            "source_draft_id": source_draft_id, "proposal": p,
        })

        # 同一 L0 + proposal 的稳定候选已存在时，不重复建 review_queue/commit。
        if not inserted:
            continue

        if risk != "low":
            conn.execute(
                "INSERT INTO review_queue(id, candidate_id, priority, risk_tier,"
                " reason, status) VALUES(?,?,?,?,?,'pending')",
                (new_id("rq"), cid, 2 if risk == "high" else 5, risk,
                 f"冷启动批量抽取（第 {chapter} 章，{risk} 风险）"),
            )
            out["queued_review"].append(cid)
            continue

        if not auto_promote_low_risk:
            out["staged_low"].append(cid)
            continue

        # 低风险自动晋升；commit 失败（如实体未建）不阻断，回落暂存区
        try:
            row = conn.execute(
                "SELECT * FROM fact_candidates WHERE candidate_id=?", (cid,)
            ).fetchone()
            cand = FactCandidate(
                candidate_id=row["candidate_id"], entity_id=row["entity_id"],
                fact_type=row["fact_type"], proposal_json=row["proposal_json"],
                status=row["status"], risk_tier=row["risk_tier"],
                source_chapter=row["source_chapter"] or 0,
                target_fact_id=row["target_fact_id"],
                evidence_refs=row["evidence_refs"],
            )
            with_retry(lambda c=cand: commit_canon(
                c, conn, policy_mode="human_gate", actor=actor))
            out["auto_promoted"].append(cid)
        except Exception as e:
            out["staged_low"].append(cid)
            out["errors"].append({"candidate_id": cid, "error": str(e)[:200]})
    conn.commit()
    return out


# ── 4. 后台导入 job ───────────────────────────────────────────────────────────

@dataclass
class ColdStartJob:
    job_id: str
    project_id: str
    status: str = "running"          # running / completed / error
    chapters_total: int = 0
    chapters_done: int = 0
    proposals_extracted: int = 0
    dedup_dropped: int = 0
    candidates_created: int = 0
    auto_promoted: int = 0
    staged_low: int = 0
    queued_review: int = 0
    error: Optional[str] = None
    started_at: str = ""
    finished_at: Optional[str] = None
    errors: list = field(default_factory=list)

    def snapshot(self) -> dict:
        return {
            "job_id": self.job_id, "project_id": self.project_id,
            "status": self.status,
            "chapters_total": self.chapters_total,
            "chapters_done": self.chapters_done,
            "proposals_extracted": self.proposals_extracted,
            "dedup_dropped": self.dedup_dropped,
            "candidates_created": self.candidates_created,
            "auto_promoted": self.auto_promoted,
            "staged_low": self.staged_low,
            "queued_review": self.queued_review,
            "error": self.error,
            "started_at": self.started_at, "finished_at": self.finished_at,
        }


class ColdStartJobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, ColdStartJob] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Optional[ColdStartJob]:
        return self._jobs.get(job_id)

    def list_for_project(self, project_id: str) -> list[ColdStartJob]:
        return sorted(
            (j for j in self._jobs.values() if j.project_id == project_id),
            key=lambda j: j.started_at, reverse=True,
        )

    def start(
        self,
        project_id: str,
        chapters: list[dict],
        registry,
        *,
        auto_promote_low_risk: bool = False,
        max_concurrency: int = 4,
        api_key: Optional[str] = None,
    ) -> ColdStartJob:
        job = ColdStartJob(
            job_id=new_id("csj"), project_id=project_id,
            chapters_total=len(chapters),
            started_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
        with self._lock:
            self._jobs[job.job_id] = job
        thread = threading.Thread(
            target=self._run, daemon=True, name=f"coldstart-{job.job_id}",
            args=(job, chapters, registry, auto_promote_low_risk,
                  max_concurrency, api_key),
        )
        thread.start()
        return job

    # LLM 抽取并发（无共享 conn），DB 写入串行在 job 线程内完成
    def _run(self, job: ColdStartJob, chapters: list[dict], registry,
             auto_promote_low_risk: bool, max_concurrency: int,
             api_key: Optional[str]) -> None:
        from concurrent.futures import ThreadPoolExecutor
        import os

        from ..config import NovelForgeConfig
        from ..control_plane.budget import BudgetLedger
        from ..control_plane.llm.factory import build_gateway
        from ..control_plane.skill_base import SkillContext
        from ..skills.cold_extract_skill import ColdExtractSkill

        try:
            cfg = NovelForgeConfig(project_id=job.project_id, db_path="")
            key = api_key or os.environ.get("DEEPSEEK_API_KEY") \
                or os.environ.get("ANTHROPIC_API_KEY")
            if key:
                cfg.provider.api_key = key
                cfg.provider.provider = os.environ.get("NOVELFORGE_PROVIDER", "deepseek")
            else:
                cfg.provider.provider = "fake"
            gw = build_gateway(cfg, ledger=BudgetLedger())
            skill = ColdExtractSkill()

            def _extract(ch: dict) -> list[dict]:
                ws = {"source_text": ch["text"], "chapter_no": ch["chapter_no"]}
                ctx = SkillContext(
                    project_id=job.project_id,
                    target_chapter=ch["chapter_no"], mode="human_gate",
                    as_of_chapter=ch["chapter_no"] - 1,
                    budget=gw.ledger, llm=gw, conn=None, workspace=ws,
                )
                try:
                    skill.run(ctx)
                except Exception as e:
                    with self._lock:
                        job.errors.append(
                            {"chapter": ch["chapter_no"], "error": str(e)[:200]})
                with self._lock:
                    job.chapters_done += 1
                proposals = ws.get("proposals", []) or []
                for proposal in proposals:
                    if isinstance(proposal, dict):
                        proposal.setdefault("valid_from_chapter", ch["chapter_no"])
                return proposals

            workers = max(1, min(8, max_concurrency))
            all_props: list[dict] = []
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="coldx") as pool:
                for props in pool.map(_extract, chapters):
                    all_props.extend(props)
            job.proposals_extracted = len(all_props)

            conn = registry.open_conn(job.project_id)
            try:
                from ..control_plane.orchestrator import _persist_draft

                project_meta = registry.get(job.project_id)
                draft_ids: dict[int, str] = {}
                chapter_texts: dict[int, str] = {}
                # 冷启动正文先进入 L0；来源原子因此可回读、可校验、可幂等。
                for ch in chapters:
                    chapter = int(ch["chapter_no"])
                    text = ch["text"]
                    chapter_texts[chapter] = text
                    source_hash = hashlib.sha256(
                        text.encode("utf-8")
                    ).hexdigest()
                    latest = conn.execute(
                        "SELECT id, sha256 FROM draft_index WHERE chapter=?"
                        " ORDER BY revision_round DESC LIMIT 1",
                        (chapter,),
                    ).fetchone()
                    if latest is not None and latest["sha256"] == source_hash:
                        draft_id = latest["id"]
                    else:
                        draft_id = _persist_draft(
                            conn, text, chapter, job.project_id,
                            project_meta.db_path if project_meta else "novel.db",
                        )
                    if draft_id is None:
                        raise RuntimeError(f"第 {chapter} 章 L0 落盘失败")
                    draft_ids[chapter] = draft_id
                    atom_id = "atom_" + hashlib.sha256(
                        f"{draft_id}:cold_start_source".encode("utf-8")
                    ).hexdigest()[:26]
                    conn.execute(
                        "INSERT INTO l1_atoms("
                        " id, chapter, draft_id, atom_text, anchor, extracted_by,"
                        " atom_type, payload_json,"
                        " source_line_start, source_line_end, content_hash, status, cold_start)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,'active',1)"
                        " ON CONFLICT(id) DO UPDATE SET status='active', anchor=excluded.anchor,"
                        " source_line_start=excluded.source_line_start,"
                        " source_line_end=excluded.source_line_end",
                        (atom_id, chapter, draft_id, text[:500], text[:500],
                         "cold_extract_batch", "cold_start_source",
                         json.dumps({"source": "cold_start_batch", "draft_id": draft_id},
                                    ensure_ascii=False),
                         1, max(1, len(text.splitlines())), source_hash),
                    )
                kept, dropped = dedup_proposals(all_props, conn)
                job.dedup_dropped = dropped
                routed = tier_route_proposals(
                    conn, kept, auto_promote_low_risk=auto_promote_low_risk,
                    source_draft_ids=draft_ids)
                from ..memory.l1_atoms import persist_proposal_atoms

                links_by_chapter: dict[int, list[dict]] = {}
                for link in routed["candidate_links"]:
                    links_by_chapter.setdefault(int(link["chapter"]), []).append(link)
                for chapter, draft_id in draft_ids.items():
                    links = links_by_chapter.get(chapter, [])
                    persist_proposal_atoms(
                        conn,
                        chapter=chapter,
                        draft_id=draft_id,
                        draft_text=chapter_texts[chapter],
                        proposals=[link["proposal"] for link in links],
                        candidate_ids=[link["candidate_id"] for link in links],
                        extracted_by="cold_extract_batch_proposal_v2",
                        cold_start=True,
                        commit=False,
                    )
                job.candidates_created = len(routed["candidate_ids"])
                job.auto_promoted = len(routed["auto_promoted"])
                job.staged_low = len(routed["staged_low"])
                job.queued_review = len(routed["queued_review"])
                job.errors.extend(routed["errors"])
                conn.commit()
            finally:
                conn.close()
            job.status = "completed"
        except Exception as e:
            job.status = "error"
            job.error = str(e)[:500]
        finally:
            job.finished_at = datetime.datetime.now(datetime.UTC).isoformat()


_manager: Optional[ColdStartJobManager] = None
_manager_lock = threading.Lock()


def get_cold_start_manager() -> ColdStartJobManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = ColdStartJobManager()
    return _manager
