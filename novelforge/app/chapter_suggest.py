"""「下一章」建议：章号推进 + 最优 chapter_goal 拼装。

被两处共用：
- GET /pipeline/next（前端「下一章 · 自动连写」按钮）
- AutopilotManager._run_loop（未显式给 chapter_goals 的章自动取最优目标）

节拍器（pacing）建议由 generate_chapter 内部自动追加，此处不重复。
"""
from __future__ import annotations

import sqlite3


def next_chapter_no(conn: sqlite3.Connection, project_id: str) -> tuple[int, int]:
    """返回 (next_chapter, last_completed_chapter)。

    last = 已完成生成（pipeline_run completed ∪ draft_index）的最大章；next = last + 1。
    """
    row = conn.execute(
        "SELECT MAX(chapter) AS c FROM pipeline_run"
        " WHERE project_id=? AND status='completed'",
        (project_id,),
    ).fetchone()
    last = row["c"] if row and row["c"] is not None else 0
    # draft_index 兜底：autopilot / 历史数据可能只登记了草稿
    row = conn.execute("SELECT MAX(chapter) AS c FROM draft_index").fetchone()
    if row and row["c"] is not None and row["c"] > last:
        last = row["c"]
    return last + 1, last


def assemble_chapter_goal(conn: sqlite3.Connection, chapter: int) -> tuple[str, list[str]]:
    """为指定章拼装最优 chapter_goal，返回 (goal, sources)。

    按优先级：本章章节卡 → 上一章钩子 → 所属卷大纲 → 到期伏笔 → 已计划节拍。
    """
    goal_parts: list[str] = []
    sources: list[str] = []

    card = conn.execute(
        "SELECT title, goal, summary FROM chapter_cards WHERE chapter=?", (chapter,)
    ).fetchone()
    if card and (card["goal"] or card["summary"]):
        prefix = f"本章《{card['title']}》：" if card["title"] else "本章目标："
        goal_parts.append(prefix + (card["goal"] or card["summary"]))
        sources.append("chapter_card")

    if chapter > 1:
        prev = conn.execute(
            "SELECT hook_text FROM chapter_cards WHERE chapter=?", (chapter - 1,)
        ).fetchone()
        if prev and prev["hook_text"]:
            goal_parts.append(f"承接上一章钩子：{prev['hook_text']}")
            sources.append("prev_hook")

    vol = conn.execute(
        "SELECT title, synopsis FROM volumes"
        " WHERE start_chapter IS NOT NULL AND start_chapter<=?"
        "   AND (end_chapter IS NULL OR end_chapter>=?)"
        " ORDER BY volume_no LIMIT 1",
        (chapter, chapter),
    ).fetchone()
    if vol and vol["synopsis"]:
        goal_parts.append(f"本卷《{vol['title']}》主线：{vol['synopsis']}")
        sources.append("volume")

    # P1#6：伏笔挂角色名下——条目带关联角色，写作时可定向安排该角色出场
    fs_rows = conn.execute(
        "SELECT f.label, f.due_chapter, e.canonical_name AS entity_name"
        " FROM foreshadow f LEFT JOIN entities e ON e.id = f.related_entity_id"
        " WHERE f.state IN ('planted','reinforced','misled','overdue')"
        "   AND f.due_chapter IS NOT NULL AND f.due_chapter<=?"
        " ORDER BY f.due_chapter LIMIT 5",
        (chapter + 2,),
    ).fetchall()
    if fs_rows:
        def _fs_label(r, suffix: str) -> str:
            who = f"角色：{r['entity_name']}，" if r["entity_name"] else ""
            return f"{r['label']}（{who}第{r['due_chapter']}章{suffix}）"

        overdue = [r for r in fs_rows if r["due_chapter"] < chapter]
        upcoming = [r for r in fs_rows if r["due_chapter"] >= chapter]
        if overdue:
            # M5-⑧：逾期伏笔置顶，必须优先处理（hookAgenda 防堆积）
            labels = "、".join(_fs_label(r, "已到期") for r in overdue)
            goal_parts.insert(0, f"【逾期伏笔，必须本章回收或推进】{labels}")
            sources.insert(0, "foreshadow_overdue")
        if upcoming:
            labels = "、".join(_fs_label(r, "到期") for r in upcoming)
            goal_parts.append(f"需要推进/回收的伏笔：{labels}")
            sources.append("foreshadow")

    beat_rows = conn.execute(
        "SELECT beat_type, summary FROM beats"
        " WHERE chapter=? AND status='planned' ORDER BY seq LIMIT 8",
        (chapter,),
    ).fetchall()
    if beat_rows:
        beats_txt = "；".join(f"[{r['beat_type']}] {r['summary']}" for r in beat_rows)
        goal_parts.append(f"本章已计划节拍：{beats_txt}")
        sources.append("beats")

    return "\n".join(goal_parts), sources
