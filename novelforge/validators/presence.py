"""P2#12 原型 — per-character visibility 单项 validator。

validate_event_visibility：「角色知道了不在场的事」——
KNOWLEDGE claim 的主语既无知情边、信息不公开，而账本里能找到对应的
timeline_events 事件且其 participants 不含主语 → KNOWLEDGE_NO_PRESENCE（major）。

与 validate_knowledge_edges（KNOWLEDGE_LEAK）互补：LEAK 查"知情集缺边"，
NO_PRESENCE 多给一层事发现场证据（在场名单具体到人），仅在能定位事件时触发。

refine_knowledge_claims：接线层精炼——正则抽出的自由文本主语/信息词
归一到 entities.id 与账本 secret_key，归一失败整条丢弃。没有它，
"陆天知道了惊天秘密"这类抽取噪声会让 LEAK 全量误报。

原型期不改 schema：不加时态边/visibility 列，评估误报率后再做世界投影改造。
"""
from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .types import WorldState

from .types import Claim, ClaimType, Issue
from .knowledge import EXEMPT_TAGS_KNOWLEDGE

_TITLE_MATCH_THRESHOLD = 0.6


def validate_event_visibility(
    claims: list, world: "WorldState", conn: sqlite3.Connection,
) -> list[Issue]:
    """Check that knowledge claims have a presence basis (P2#12 prototype)."""
    issues: list[Issue] = []
    for c in [c for c in claims if c.ctype == ClaimType.KNOWLEDGE]:
        if set(c.exempt_tags) & EXEMPT_TAGS_KNOWLEDGE:
            continue
        info_key = c.payload.get("info_key", "")
        if not info_key:
            continue
        ent_id = _resolve_entity(c.subject_entity, conn)
        if ent_id is None:
            continue   # 抽取噪声不报

        # 已有知情边（被转告/亲历已记账）或信息已公开 → 合法
        if info_key in world.knowledge_set(ent_id, c.chapter):
            continue
        if world.is_public(info_key, c.chapter):
            continue

        events = _matching_events(info_key, c.chapter, conn)
        if not events:
            continue   # 无在场证据可查 → 归 KNOWLEDGE_LEAK 兜底，不重复报
        names = _entity_names(ent_id, conn)
        if any(_is_participant(names, ent_id, ev["participants"]) for ev in events):
            continue   # 亲历

        ev = events[0]
        roster = _roster_preview(ev["participants"], conn)
        issues.append(Issue(
            code="KNOWLEDGE_NO_PRESENCE", severity="major", kind="hard",
            claim_id=c.claim_id, chapter=c.chapter,
            message=(f"{c.subject_entity} 在第{c.chapter}章提及「{info_key}」，"
                     f"但该事发生于第{ev['chapter']}章且其不在场"
                     f"（在场：{roster or '无记录'}），也无知情边/公开记录"),
            evidence_refs=[c.claim_id],
            suggested_fix=(f"若有人转告，请补 knowledge_edges({c.subject_entity}, "
                           f"{info_key}, ≤{c.chapter})；否则改写为道听途说或删去"),
        ))
    return issues


def refine_knowledge_claims(claims: list, conn: sqlite3.Connection) -> list[Claim]:
    """KNOWLEDGE claims 精炼：主语→entities.id，info_key→账本 secret_key。

    任一归一失败整条丢弃（宁漏勿误报——正则抽取召回优先，过滤在这里做）。
    返回新列表，不修改入参。
    """
    keys = [r[0] for r in conn.execute(
        "SELECT DISTINCT secret_key FROM knowledge_edges").fetchall()]
    out: list[Claim] = []
    for c in claims:
        if c.ctype != ClaimType.KNOWLEDGE:
            continue
        ent_id = _resolve_entity(c.subject_entity, conn)
        if ent_id is None:
            continue
        key = _canonical_key(c.payload.get("info_key", ""), keys)
        if key is None:
            continue
        out.append(c.model_copy(update={
            "subject_entity": ent_id,
            "payload": {**c.payload, "info_key": key},
        }))
    return out


# ── helpers ───────────────────────────────────────────────────────────────────

def _resolve_entity(ref: Optional[str], conn: sqlite3.Connection) -> Optional[str]:
    if not ref:
        return None
    if conn.execute("SELECT 1 FROM entities WHERE id=?", (ref,)).fetchone():
        return ref
    row = conn.execute(
        "SELECT id FROM entities WHERE canonical_name=?", (ref,)).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT entity_id FROM entity_aliases WHERE alias=?", (ref,)).fetchone()
    return row[0] if row else None


def _entity_names(ent_id: str, conn: sqlite3.Connection) -> set[str]:
    names = {ent_id}
    row = conn.execute(
        "SELECT canonical_name FROM entities WHERE id=?", (ent_id,)).fetchone()
    if row:
        names.add(row[0])
    for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id=?", (ent_id,)).fetchall():
        names.add(r[0])
    return names


def _matching_events(info_key: str, chapter: int, conn: sqlite3.Connection) -> list[dict]:
    """chapter 之前（含当章）标题与 info_key 匹配的事件：互相包含或 bigram ≥0.6。"""
    rows = conn.execute(
        "SELECT title, chapter, participants FROM timeline_events"
        " WHERE chapter<=? ORDER BY chapter", (chapter,)).fetchall()
    out = []
    for r in rows:
        title = r["title"] or ""
        if (info_key in title or title in info_key
                or _bigram_overlap(info_key, title) >= _TITLE_MATCH_THRESHOLD):
            out.append({"title": title, "chapter": r["chapter"],
                        "participants": _parse_participants(r["participants"])})
    return out


def _parse_participants(raw) -> list[str]:
    if not raw:
        return []
    try:
        parts = json.loads(raw)
        return [str(p) for p in parts] if isinstance(parts, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _is_participant(names: set[str], ent_id: str, participants: list[str]) -> bool:
    return bool(names & set(participants)) or ent_id in participants


def _roster_preview(participants: list[str], conn: sqlite3.Connection) -> str:
    shown = []
    for p in participants[:5]:
        row = conn.execute(
            "SELECT canonical_name FROM entities WHERE id=?", (p,)).fetchone()
        shown.append(row[0] if row else p)
    return "、".join(shown)


def _canonical_key(raw: str, keys: list[str]) -> Optional[str]:
    if not raw:
        return None
    if raw in keys:
        return raw
    best, best_score = None, 0.0
    for k in keys:
        if raw in k or k in raw:
            return k
        s = _bigram_overlap(raw, k)
        if s > best_score:
            best, best_score = k, s
    return best if best_score >= _TITLE_MATCH_THRESHOLD else None


def _bigrams(s: str) -> set[str]:
    s = "".join(s.split())
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}


def _bigram_overlap(a: str, b: str) -> float:
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / min(len(ba), len(bb))
