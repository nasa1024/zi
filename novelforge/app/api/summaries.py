"""三层摘要看板端点（POST-v15 P2#7）。

后端早已具备三层摘要能力（orchestrator 结算：章摘要 → 卷 rolling_summary →
meta_kv global_synopsis），但只写不读、不可编辑。本路由把三层暴露为看板数据
源，并允许作者直接改写任一层——**编辑即改真相源**：改过的章摘要会被后续
recall/卷 rollup 消费。

路由：
  GET   /{project_id}/summaries                       → SummaryBoardResponse
  PATCH /{project_id}/summaries/global                → 覆写全局梗概
  PATCH /{project_id}/summaries/volumes/{volume_no}   → 覆写卷滚动摘要
  PATCH /{project_id}/summaries/chapters/{chapter}    → upsert 章摘要
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import ProjectRegistry, get_registry
from ..models import (
    ChapterSummaryUpdateRequest, GlobalSynopsisUpdateRequest,
    SummaryBoardResponse, SummaryChapterItem, SummaryVolumeItem,
    VolumeSummaryUpdateRequest,
)
from ...db.connection import get_meta, set_meta
from ...ids import new_id

router = APIRouter(tags=["summaries"])


@router.get("/{project_id}/summaries", response_model=SummaryBoardResponse)
def summary_board(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        vols = conn.execute(
            "SELECT volume_no, title, rolling_summary, start_chapter,"
            "       end_chapter, status FROM volumes ORDER BY volume_no"
        ).fetchall()
        chapters = conn.execute(
            "SELECT cs.chapter, cs.summary, cs.volume_no, cs.created_at,"
            "       cc.title,"
            "       (SELECT word_count FROM draft_index di WHERE di.chapter=cs.chapter"
            "        ORDER BY revision_round DESC LIMIT 1) AS word_count"
            " FROM chapter_summaries cs"
            " LEFT JOIN chapter_cards cc ON cc.chapter=cs.chapter"
            " ORDER BY cs.chapter"
        ).fetchall()
        return SummaryBoardResponse(
            global_synopsis=get_meta(conn, "global_synopsis"),
            volumes=[SummaryVolumeItem(
                volume_no=v["volume_no"], title=v["title"],
                rolling_summary=v["rolling_summary"],
                start_chapter=v["start_chapter"], end_chapter=v["end_chapter"],
                status=v["status"]) for v in vols],
            chapters=[SummaryChapterItem(
                chapter=c["chapter"], summary=c["summary"],
                volume_no=c["volume_no"], title=c["title"],
                word_count=c["word_count"], created_at=c["created_at"])
                for c in chapters],
        )
    finally:
        conn.close()


@router.patch("/{project_id}/summaries/global")
def update_global_synopsis(
    project_id: str,
    req: GlobalSynopsisUpdateRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        set_meta(conn, "global_synopsis", req.synopsis.strip())
        conn.commit()
        return {"global_synopsis": req.synopsis.strip()}
    finally:
        conn.close()


@router.patch("/{project_id}/summaries/volumes/{volume_no}")
def update_volume_summary(
    project_id: str,
    volume_no: int,
    req: VolumeSummaryUpdateRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        cur = conn.execute(
            "UPDATE volumes SET rolling_summary=? WHERE volume_no=?",
            (req.rolling_summary.strip(), volume_no))
        if cur.rowcount == 0:
            raise HTTPException(404, f"volume_no={volume_no} 不存在")
        conn.commit()
        return {"volume_no": volume_no, "rolling_summary": req.rolling_summary.strip()}
    finally:
        conn.close()


@router.patch("/{project_id}/summaries/chapters/{chapter}",
              response_model=SummaryChapterItem)
def update_chapter_summary(
    project_id: str,
    chapter: int,
    req: ChapterSummaryUpdateRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    """upsert 章摘要（真相源）：改后的摘要被 recall 前情窗口与卷 rollup 消费。"""
    conn = registry.open_conn(project_id)
    try:
        vol = conn.execute(
            "SELECT volume_no FROM volumes"
            " WHERE start_chapter IS NOT NULL AND start_chapter<=?"
            "   AND (end_chapter IS NULL OR end_chapter>=?)"
            " ORDER BY volume_no LIMIT 1", (chapter, chapter)).fetchone()
        volume_no = vol["volume_no"] if vol else None
        conn.execute(
            "INSERT INTO chapter_summaries(id, chapter, summary, volume_no)"
            " VALUES(?,?,?,?)"
            " ON CONFLICT(chapter) DO UPDATE SET"
            "   summary=excluded.summary, created_at=datetime('now')",
            (new_id("csum"), chapter, req.summary.strip(), volume_no))
        conn.commit()
        row = conn.execute(
            "SELECT cs.chapter, cs.summary, cs.volume_no, cs.created_at, cc.title,"
            "       (SELECT word_count FROM draft_index di WHERE di.chapter=cs.chapter"
            "        ORDER BY revision_round DESC LIMIT 1) AS word_count"
            " FROM chapter_summaries cs"
            " LEFT JOIN chapter_cards cc ON cc.chapter=cs.chapter"
            " WHERE cs.chapter=?", (chapter,)).fetchone()
        return SummaryChapterItem(
            chapter=row["chapter"], summary=row["summary"],
            volume_no=row["volume_no"], title=row["title"],
            word_count=row["word_count"], created_at=row["created_at"])
    finally:
        conn.close()
