"""从 facts 表确定性渲染 story_bible（只读，§8.2.12）。

输出格式：markdown（默认）或 json。
永不被 LLM 写回（硬原则 2）。
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Literal


def render_bible(
    conn: sqlite3.Connection,
    *,
    as_of_chapter: int = 99999,
    fmt: Literal["markdown", "json"] = "markdown",
) -> tuple[str, dict]:
    """渲染 story bible，返回 (content_str, stats_dict)。"""
    rows = conn.execute(
        "SELECT f.id, f.entity_id, f.fact_type, f.subject, f.predicate, f.object,"
        "       f.valid_from_chapter, f.valid_to_chapter, e.canonical_name"
        " FROM facts f"
        " LEFT JOIN entities e ON e.id=f.entity_id"
        " WHERE f.status='canon' AND f.valid_from_chapter<=?"
        "   AND (f.valid_to_chapter IS NULL OR f.valid_to_chapter>?)"
        " ORDER BY f.entity_id, f.fact_type, f.valid_from_chapter",
        (as_of_chapter, as_of_chapter),
    ).fetchall()

    # 按 entity 分组
    by_entity: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        name = r["canonical_name"] or r["subject"] or "全局"
        by_entity[name].append({
            "id": r["id"],
            "fact_type": r["fact_type"],
            "predicate": r["predicate"],
            "object": r["object"],
            "valid_from_chapter": r["valid_from_chapter"],
        })

    stats = {"facts": len(rows), "as_of_chapter": as_of_chapter, "entities": len(by_entity)}

    if fmt == "json":
        return json.dumps({"entities": dict(by_entity)}, ensure_ascii=False, indent=2), stats

    # Markdown 渲染
    lines = [f"# Story Bible（as of ch.{as_of_chapter}）\n"]
    lines.append(f"> 本文档由 `facts` 表确定性渲染，只读。共 {len(rows)} 条 canon facts。\n")

    for entity_name, facts in sorted(by_entity.items()):
        lines.append(f"\n## {entity_name}\n")
        by_type: dict[str, list] = defaultdict(list)
        for f in facts:
            by_type[f["fact_type"]].append(f)
        for ft, flist in sorted(by_type.items()):
            lines.append(f"\n### {ft}\n")
            for f in flist:
                lines.append(f"- **{f['predicate']}**: {f['object']}  "
                              f"*(ch.{f['valid_from_chapter']})*\n")

    return "".join(lines), stats
