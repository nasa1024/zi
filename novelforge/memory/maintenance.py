"""记忆层健康检查与可重放维护任务。"""
from __future__ import annotations

import sqlite3

from ..config import NovelForgeConfig, effective_memory_settings
from ..db.connection import rebuild_drafts_fts
from ..db.l0 import load_latest_draft_text, resolve_draft_path
from .embedding import build_embedder
from .l1_atoms import rebuild_l1_atoms
from .l2_aggregate import run_l2_window
from .vector_store import SceneVectorStore


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] if row is not None else 0)


def _coverage(done: int, total: int) -> float:
    return min(100.0, round(done * 100.0 / total, 1)) if total else 100.0


def memory_health(conn: sqlite3.Connection, cfg: NovelForgeConfig) -> dict:
    latest = conn.execute(
        "SELECT di.id, di.chapter, di.file_path, di.fts_indexed_at,"
        " di.l1_status, di.l2_status FROM draft_index di"
        " JOIN (SELECT chapter, MAX(revision_round) AS revision_round"
        "       FROM draft_index GROUP BY chapter) x"
        " ON x.chapter=di.chapter AND x.revision_round=di.revision_round"
        " ORDER BY di.chapter"
    ).fetchall()
    present = missing = 0
    present_draft_ids: list[str] = []
    for row in latest:
        try:
            exists = resolve_draft_path(conn, row["file_path"]).exists()
        except (TypeError, ValueError, OSError):
            exists = False
        if exists:
            present += 1
            present_draft_ids.append(row["id"])
        else:
            missing += 1

    draft_chapters = len(latest)
    fts_chapters = _count(conn, "SELECT COUNT(DISTINCT chapter_no) FROM drafts_fts")
    l1_completed = sum(1 for row in latest if row["l1_status"] == "completed")
    l1_failed = sum(1 for row in latest if row["l1_status"] == "failed")
    l2_completed = sum(1 for row in latest if row["l2_status"] == "completed")
    l2_failed = sum(1 for row in latest if row["l2_status"] == "failed")
    latest_ids = [row["id"] for row in latest]
    latest_ph = ",".join("?" * len(latest_ids)) or "NULL"
    present_ph = ",".join("?" * len(present_draft_ids)) or "NULL"
    l1_atoms = _count(
        conn, f"SELECT COUNT(*) FROM l1_atoms WHERE draft_id IN ({latest_ph})",
        tuple(latest_ids),
    )
    l1_chapters = _count(
        conn,
        f"SELECT COUNT(DISTINCT chapter) FROM l1_atoms"
        f" WHERE draft_id IN ({latest_ph})",
        tuple(latest_ids),
    )
    l1_anchored = _count(
        conn,
        f"SELECT COUNT(*) FROM l1_atoms WHERE draft_id IN ({present_ph})"
        " AND status='active' AND source_line_start IS NOT NULL"
        " AND source_line_end IS NOT NULL AND source_line_start>=1"
        " AND source_line_end>=source_line_start",
        tuple(present_draft_ids),
    )
    l1_unanchored = max(0, l1_atoms - l1_anchored)
    l1_total_rows = _count(conn, "SELECT COUNT(*) FROM l1_atoms")
    l1_superseded = _count(
        conn, "SELECT COUNT(*) FROM l1_atoms WHERE status='superseded'"
    )
    l2_scenes = _count(conn, "SELECT COUNT(*) FROM l2_scenes")
    l2_chapters = _count(conn, "SELECT COUNT(DISTINCT chapter) FROM l2_scenes")
    embedder = build_embedder(cfg.memory, dim=cfg.memory.embedding_dim)
    expected_dim = int(getattr(embedder, "dim", 0) or 0)
    vector_rows = conn.execute(
        "SELECT s.id, s.embedding_model, s.embedding_dim, s.embedding_version,"
        " s.indexed_at, v.scene_id, v.dim, v.model, v.version"
        " FROM l2_scenes s LEFT JOIN scene_vec v ON v.scene_id=s.id"
    ).fetchall()
    vectors = _count(conn, "SELECT COUNT(*) FROM scene_vec")
    usable_vectors = 0
    stale_vectors = 0
    for row in vector_rows:
        if row["scene_id"] is None:
            continue
        usable = (
            (row["model"] or "") == embedder.name
            and (row["version"] or "") == embedder.version
            and int(row["dim"] or 0) > 0
            and (expected_dim <= 0 or int(row["dim"]) == expected_dim)
            and row["embedding_model"] == row["model"]
            and row["embedding_version"] == row["version"]
            and row["embedding_dim"] == row["dim"]
            and bool(row["indexed_at"])
        )
        if usable:
            usable_vectors += 1
        else:
            stale_vectors += 1
    orphan_vectors = _count(
        conn,
        "SELECT COUNT(*) FROM scene_vec v"
        " LEFT JOIN l2_scenes s ON s.id=v.scene_id WHERE s.id IS NULL",
    )
    summaries = _count(conn, "SELECT COUNT(DISTINCT chapter) FROM chapter_summaries")
    warnings: list[str] = []
    if missing:
        warnings.append(f"{missing} 个最新 L0 文件缺失")
    if fts_chapters < draft_chapters:
        warnings.append(f"L0 FTS 尚有 {draft_chapters - fts_chapters} 章未索引")
    if l1_completed < draft_chapters:
        warnings.append(f"L1 尚有 {draft_chapters - l1_completed} 章未完成")
    if l1_unanchored:
        warnings.append(f"L1 有 {l1_unanchored} 条原子缺少可核验证据行")
    if cfg.memory.enable_l2 and l2_completed < draft_chapters:
        warnings.append(f"L2 尚有 {draft_chapters - l2_completed} 章待聚合")
    if cfg.recall.enable_summaries and summaries < draft_chapters:
        warnings.append(f"叙事摘要尚有 {draft_chapters - summaries} 章未生成")
    if cfg.recall.enable_vector and usable_vectors < l2_scenes:
        warnings.append(f"当前模型可用场景向量尚缺 {l2_scenes - usable_vectors} 条")
    if stale_vectors:
        warnings.append(f"场景向量有 {stale_vectors} 条模型或元数据已过期")
    if orphan_vectors:
        warnings.append(f"场景向量有 {orphan_vectors} 条孤儿记录")

    return {
        "latest_chapter": max((row["chapter"] for row in latest), default=0),
        "settings": effective_memory_settings(cfg),
        "layers": {
            "l0": {
                "rows": _count(conn, "SELECT COUNT(*) FROM draft_index"),
                "chapters": draft_chapters,
                "completed_chapters": present,
                "coverage_pct": _coverage(present, draft_chapters),
                "details": {
                    "files_present": present,
                    "files_missing": missing,
                    "fts_chunks": _count(conn, "SELECT COUNT(*) FROM drafts_fts"),
                    "fts_chapters": fts_chapters,
                    "fts_coverage_pct": _coverage(fts_chapters, draft_chapters),
                },
            },
            "l1": {
                "rows": l1_atoms,
                "chapters": l1_chapters,
                "completed_chapters": l1_completed,
                "coverage_pct": _coverage(l1_completed, draft_chapters),
                "details": {
                    "anchored": l1_anchored,
                    "unanchored": l1_unanchored,
                    "failed_chapters": l1_failed,
                    "total_rows": l1_total_rows,
                    "superseded": l1_superseded,
                },
            },
            "l2": {
                "rows": l2_scenes,
                "chapters": l2_chapters,
                "completed_chapters": l2_completed,
                "coverage_pct": _coverage(l2_completed, draft_chapters),
                "details": {
                    "vectors": usable_vectors,
                    "vector_rows": vectors,
                    "stale_vectors": stale_vectors,
                    "orphan_vectors": orphan_vectors,
                    "vector_coverage_pct": _coverage(usable_vectors, l2_scenes),
                    "failed_chapters": l2_failed,
                },
            },
            "summaries": {
                "rows": summaries,
                "chapters": summaries,
                "completed_chapters": summaries,
                "coverage_pct": _coverage(summaries, draft_chapters),
                "details": {},
            },
        },
        "warnings": warnings,
    }


def rebuild_memory(
    conn: sqlite3.Connection,
    cfg: NovelForgeConfig,
    *,
    rebuild_l0: bool = True,
    rebuild_l1: bool = True,
    rebuild_l2: bool = True,
    rebuild_vectors: bool = True,
    force: bool = False,
    gateway=None,
) -> dict:
    """同步维护入口；所有步骤均可从 L0/业务表重放。"""
    result: dict[str, object] = {}
    if rebuild_l0:
        result["l0"] = rebuild_drafts_fts(conn)
    if rebuild_l1:
        result["l1"] = rebuild_l1_atoms(conn, force=force)

    embedder = build_embedder(cfg.memory, dim=cfg.memory.embedding_dim) if rebuild_vectors else None
    if rebuild_l2:
        up_to = _count(conn, "SELECT COALESCE(MAX(chapter),0) FROM draft_index")
        result["l2"] = run_l2_window(
            conn,
            gateway,
            up_to_chapter=up_to,
            draft_loader=lambda chapter: load_latest_draft_text(conn, chapter),
            max_scenes=cfg.memory.l2_max_scenes_per_chapter,
            tier=cfg.memory.l2_tier,
            # 向量在下一阶段统一建一次，避免外部 embedding 双倍调用/费用。
            embedder=None,
            force=force,
            preserve_existing_summaries=gateway is None,
            invalidate_vectors=not rebuild_vectors,
        )

    if rebuild_vectors:
        store = SceneVectorStore(conn, embedder)
        rows = conn.execute(
            "SELECT id, chapter, summary, scene_text FROM l2_scenes ORDER BY chapter, scene_seq"
        ).fetchall()
        indexed = 0
        try:
            conn.execute("SAVEPOINT rebuild_scene_vectors")
            for row in rows:
                metadata = store.upsert(
                    row["id"], row["chapter"], row["summary"] or row["scene_text"]
                )
                conn.execute(
                    "UPDATE l2_scenes SET embedding_model=?, embedding_dim=?,"
                    " embedding_version=?, indexed_at=datetime('now') WHERE id=?",
                    (metadata["model"], metadata["dim"], metadata["version"], row["id"]),
                )
                indexed += 1
            conn.execute(
                "DELETE FROM scene_vec WHERE scene_id NOT IN (SELECT id FROM l2_scenes)"
            )
            conn.execute("RELEASE SAVEPOINT rebuild_scene_vectors")
            conn.commit()
        except Exception:
            try:
                conn.execute("ROLLBACK TO SAVEPOINT rebuild_scene_vectors")
                conn.execute("RELEASE SAVEPOINT rebuild_scene_vectors")
            except sqlite3.Error:
                conn.rollback()
            raise
        result["vectors"] = {"indexed": indexed}
    result["health"] = memory_health(conn, cfg)
    return result
