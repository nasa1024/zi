"""L2 场景聚合（§06.2.2）。

把每章正文切成"场景块"，各生成短摘要写入 l2_scenes——这是向量召回（§06.4.3
"查相似桥段"）的语料层。触发：§06 规定按卷/弧线边界 或 每 3-5 章 或 idle；本模块
提供确定性切分 + 单章聚合 + 窗口回填，触发判定交由 orchestrator（卷边界）+ should_run_l2。

切分纯确定性（分隔线/空行分段 + 合并过短 + 封顶），不调 LLM；只有"场景摘要"调
FAST 档。无 gateway 时降级为截断原文当摘要，仍可建语料/向量。
"""
from __future__ import annotations

import re
import sqlite3
from typing import Callable, Optional

from ..ids import new_id
from .embedding import Embedder
from .vector_store import SceneVectorStore

# 仅由分隔符/空白构成的整行 → 场景分隔（***、———、※、=== 等）
_SEP_LINE_RE = re.compile(r"^[\s\*\-—─=~·•※◆◇＊#＝]+$")
_PARA_SPLIT_RE = re.compile(r"\n\s*\n")

_L2_SCENE_SYSTEM = (
    "你是 NovelForge 的场景速记助手。把给定的小说场景原文压缩成不超过 80 字的"
    "客观摘要：发生地点、在场人物、关键动作或转折。只输出摘要正文，不要点评。"
)

_TIER_MAP = {"fast": "FAST", "mid": "MID", "strong": "STRONG"}


# ── 确定性切分 ────────────────────────────────────────────────────────────────

def split_scenes(text: str, *, max_scenes: int = 6, min_chars: int = 40) -> list[str]:
    """把章正文切成 1..max_scenes 个场景块。确定性、无 LLM。

    优先按分隔线切；无分隔线则按空行分段。过短片段并入相邻块；超出 max_scenes
    的尾部块并入最后一块（不丢内容）。
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    # 显式分隔线是作者的强场景信号 → 尊重，只封顶不按长度合并。
    marked = _split_on_markers(text)
    if len(marked) > 1:
        return _cap(marked, max_scenes)

    # 无分隔线：按空行分段，过短片段并入相邻块，再封顶。
    blocks = _split_paragraphs(text)
    blocks = _merge_short(blocks, min_chars)
    return _cap(blocks, max_scenes)


def _split_on_markers(text: str) -> list[str]:
    blocks: list[str] = []
    buf: list[str] = []
    for line in text.split("\n"):
        s = line.strip()
        if s and _SEP_LINE_RE.match(s) and len(s) >= 2:
            if buf:
                blocks.append("\n".join(buf).strip())
                buf = []
            continue
        buf.append(line)
    if buf:
        blocks.append("\n".join(buf).strip())
    return [b for b in blocks if b]


def _split_paragraphs(text: str) -> list[str]:
    paras = [p.strip() for p in _PARA_SPLIT_RE.split(text) if p.strip()]
    return paras or [text]


def _merge_short(blocks: list[str], min_chars: int) -> list[str]:
    out: list[str] = []
    for b in blocks:
        if out and len(b) < min_chars:
            out[-1] = out[-1] + "\n" + b
        else:
            out.append(b)
    # 首块本身过短：并入下一块
    if len(out) >= 2 and len(out[0]) < min_chars:
        out[1] = out[0] + "\n" + out[1]
        out = out[1:]
    return out


def _cap(blocks: list[str], max_scenes: int) -> list[str]:
    if max_scenes >= 1 and len(blocks) > max_scenes:
        head = blocks[: max_scenes - 1]
        tail = "\n".join(blocks[max_scenes - 1:])
        return head + [tail]
    return blocks


# ── 单章聚合 ──────────────────────────────────────────────────────────────────

def aggregate_chapter_scenes(
    conn: sqlite3.Connection,
    gateway,
    chapter: int,
    draft_text: str,
    *,
    max_scenes: int = 6,
    tier: str = "fast",
    embedder: Optional[Embedder] = None,
    summarize: bool = True,
    preserve_existing_summaries: bool = False,
    invalidate_vectors: bool = True,
) -> int:
    """把一章切场景、生成摘要写 l2_scenes（幂等）；embedder 给定则同步建向量。

    Returns 写入的场景数（空正文返回 0）。
    """
    scenes = split_scenes(draft_text, max_scenes=max_scenes)

    try:
        conn.execute("SAVEPOINT l2_chapter")
        store = SceneVectorStore(conn, embedder) if embedder is not None else None
        for seq, scene_text in enumerate(scenes):
            existing = conn.execute(
                "SELECT id, scene_text, summary FROM l2_scenes"
                " WHERE chapter=? AND scene_seq=?",
                (chapter, seq),
            ).fetchone()
            if summarize and gateway is not None:
                summary = _summarize(gateway, tier, scene_text) or _fallback_summary(scene_text)
            elif (
                preserve_existing_summaries
                and existing is not None
                and existing["scene_text"] == scene_text
                and existing["summary"]
            ):
                # 无 gateway 的维护重建不应把已有高质量摘要静默降级为截断原文。
                summary = existing["summary"]
            else:
                summary = _fallback_summary(scene_text)
            content_changed = (
                existing is None
                or existing["scene_text"] != scene_text
                or (existing["summary"] or "") != (summary or "")
            )
            scene_id = _upsert_scene(conn, chapter, seq, scene_text, summary)
            if store is not None and scene_id:
                metadata = store.upsert(scene_id, chapter, summary or scene_text)
                conn.execute(
                    "UPDATE l2_scenes SET embedding_model=?, embedding_dim=?,"
                    " embedding_version=?, indexed_at=datetime('now') WHERE id=?",
                    (metadata["model"], metadata["dim"], metadata["version"], scene_id),
                )
            elif scene_id and invalidate_vectors and content_changed:
                # 正文/摘要已变化但本轮未建向量，旧向量必须失效。
                conn.execute("DELETE FROM scene_vec WHERE scene_id=?", (scene_id,))
                conn.execute(
                    "UPDATE l2_scenes SET embedding_model=NULL, embedding_dim=NULL,"
                    " embedding_version=NULL, indexed_at=NULL WHERE id=?",
                    (scene_id,),
                )
            elif scene_id and content_changed:
                # 后续会统一重建向量时先保留旧 vec 便于事务失败回滚，但清空场景侧
                # 元数据，使检索/health 不会把与新正文不匹配的旧向量当成可用。
                conn.execute(
                    "UPDATE l2_scenes SET embedding_model=NULL, embedding_dim=NULL,"
                    " embedding_version=NULL, indexed_at=NULL WHERE id=?",
                    (scene_id,),
                )

        # 修订稿可能比旧稿场景更少；清掉尾部陈旧场景及其向量。
        stale = conn.execute(
            "SELECT id FROM l2_scenes WHERE chapter=? AND scene_seq>=?",
            (chapter, len(scenes)),
        ).fetchall()
        if stale:
            stale_ids = [row["id"] for row in stale]
            ph = ",".join("?" * len(stale_ids))
            conn.execute(f"DELETE FROM scene_vec WHERE scene_id IN ({ph})", stale_ids)
            conn.execute(f"DELETE FROM l2_scenes WHERE id IN ({ph})", stale_ids)
        try:
            conn.execute(
                "UPDATE draft_index SET l2_status='completed',"
                " l2_aggregated_at=datetime('now')"
                " WHERE id=(SELECT id FROM draft_index WHERE chapter=?"
                "           ORDER BY revision_round DESC LIMIT 1)",
                (chapter,),
            )
        except sqlite3.Error:
            pass
        conn.execute("RELEASE SAVEPOINT l2_chapter")
        conn.commit()
        return len(scenes)
    except Exception:
        try:
            conn.execute("ROLLBACK TO SAVEPOINT l2_chapter")
            conn.execute("RELEASE SAVEPOINT l2_chapter")
        except sqlite3.Error:
            conn.rollback()
        try:
            conn.execute(
                "UPDATE draft_index SET l2_status='failed'"
                " WHERE id=(SELECT id FROM draft_index WHERE chapter=?"
                "           ORDER BY revision_round DESC LIMIT 1)",
                (chapter,),
            )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
        raise


def _summarize(gateway, tier: str, scene_text: str) -> str:
    from ..control_plane.llm.provider import Message
    from ..control_plane.llm.tiers import ModelTier

    mt = getattr(ModelTier, _TIER_MAP.get(tier, "FAST"))
    try:
        resp = gateway.generate(
            mt,
            [Message(role="user", content=f"场景原文：\n\n{scene_text[:4000]}")],
            system=_L2_SCENE_SYSTEM,
            max_tokens=200,
        )
        return resp.text.strip()
    except Exception:
        return ""


def _fallback_summary(scene_text: str, limit: int = 80) -> str:
    """LLM 不可用时：取首段压平为单行截断当摘要。"""
    flat = " ".join(scene_text.split())
    return flat[:limit]


def _upsert_scene(
    conn: sqlite3.Connection, chapter: int, seq: int, scene_text: str, summary: str,
) -> str:
    """按 (chapter, scene_seq) 幂等写入，返回场景 id（已存在则复用其 id）。"""
    row = conn.execute(
        "SELECT id FROM l2_scenes WHERE chapter=? AND scene_seq=?", (chapter, seq)
    ).fetchone()
    if row:
        sid = row["id"]
        conn.execute(
            "UPDATE l2_scenes SET scene_text=?, summary=? WHERE id=?",
            (scene_text, summary, sid),
        )
        return sid
    sid = new_id("l2")
    conn.execute(
        "INSERT INTO l2_scenes(id, chapter, scene_seq, scene_text, summary)"
        " VALUES(?,?,?,?,?)",
        (sid, chapter, seq, scene_text, summary),
    )
    return sid


# ── 触发判定 + 窗口回填 ───────────────────────────────────────────────────────

def should_run_l2(chapter: int, *, every_n: int = 4) -> bool:
    """§06.2.2 每 N 章兜底触发（卷/弧线边界由 orchestrator 另行 OR）。"""
    return chapter > 0 and every_n > 0 and chapter % every_n == 0


def pending_l2_chapters(
    conn: sqlite3.Connection, up_to_chapter: int, *, force: bool = False,
) -> list[int]:
    """返回最新 draft 尚未完整聚合的章节；force=True 时返回全部最新章节。"""
    rows = conn.execute(
        "SELECT di.chapter FROM draft_index di"
        " JOIN (SELECT chapter, MAX(revision_round) AS revision_round"
        "       FROM draft_index WHERE chapter<=? GROUP BY chapter) latest"
        "   ON latest.chapter=di.chapter AND latest.revision_round=di.revision_round"
        " WHERE ?=1 OR di.l2_status<>'completed'"
        "    OR (COALESCE(di.word_count,1)>0 AND NOT EXISTS ("
        "        SELECT 1 FROM l2_scenes s WHERE s.chapter=di.chapter))"
        " ORDER BY di.chapter",
        (up_to_chapter, 1 if force else 0),
    ).fetchall()
    return [r["chapter"] for r in rows]


def run_l2_window(
    conn: sqlite3.Connection,
    gateway,
    up_to_chapter: int,
    draft_loader: Callable[[int], Optional[str]],
    *,
    max_scenes: int = 6,
    tier: str = "fast",
    embedder: Optional[Embedder] = None,
    force: bool = False,
    preserve_existing_summaries: bool = False,
    invalidate_vectors: bool = True,
) -> dict:
    """回填窗口内所有缺 L2 的章。draft_loader(chapter)→正文（None 则跳过）。"""
    processed = scenes = missing_files = 0
    for ch in pending_l2_chapters(conn, up_to_chapter, force=force):
        text = draft_loader(ch)
        if text is None:
            missing_files += 1
            try:
                conn.execute(
                    "UPDATE draft_index SET l2_status='failed'"
                    " WHERE id=(SELECT id FROM draft_index WHERE chapter=?"
                    "           ORDER BY revision_round DESC LIMIT 1)",
                    (ch,),
                )
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
            continue
        n = aggregate_chapter_scenes(
            conn, gateway, ch, text, max_scenes=max_scenes, tier=tier,
            embedder=embedder,
            preserve_existing_summaries=preserve_existing_summaries,
            invalidate_vectors=invalidate_vectors,
        )
        processed += 1
        scenes += n
    return {"processed": processed, "scenes": scenes, "missing_files": missing_files}
