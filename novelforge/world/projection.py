"""Projection applier: StateTransition → World State *_log writes.

I-PROJ invariant: *_log sole writer = commit_canon → project_to_world_state().
Every *_log row must have source_fact_id pointing to a status='canon' fact.
Design: §16.5 / §16.8.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from ..contracts import HARD_FACET, StateTransition
from ..ids import new_id


class ProjectionError(Exception):
    """投影前置不满足（实体/地点未建 / rank 未注册 / 枚举非法 / 双花）。
    apply_gate_routes 捕获后转人审，绝不静默吞（评审 E5）。"""


# ── 公共解析助手 ──────────────────────────────────────────────────────────────

def resolve_entity_id(ref: str, conn: sqlite3.Connection) -> str:
    """按 id → canonical_name → alias 顺序归一到 entities.id，失败 fail-fast。"""
    if ref and conn.execute("SELECT 1 FROM entities WHERE id=?", (ref,)).fetchone():
        return ref
    row = conn.execute("SELECT id FROM entities WHERE canonical_name=?", (ref,)).fetchone()
    if row:
        return row["id"]
    row = conn.execute("SELECT entity_id FROM entity_aliases WHERE alias=?", (ref,)).fetchone()
    if row:
        return row["entity_id"]
    raise ProjectionError(
        f"未知实体 '{ref}'：实体须由 entities 表/seed/更早的 add-fact 先建"
    )


def resolve_location_id(ref: str, conn: sqlite3.Connection) -> str:
    """timeline_events.location_id 外键指向 geo_locations(id)，不可用 resolve_entity_id。"""
    if ref and conn.execute("SELECT 1 FROM geo_locations WHERE id=?", (ref,)).fetchone():
        return ref
    row = conn.execute("SELECT id FROM geo_locations WHERE name=?", (ref,)).fetchone()
    if row:
        return row["id"]
    raise ProjectionError(f"未知地点 '{ref}'：geo_locations 须先建（地点不是 entity）")


def _check_enum(col: str, value: str, allowed: set[str]) -> str:
    if value not in allowed:
        raise ProjectionError(f"{col}='{value}' 不在合法集 {sorted(allowed)}")
    return value


def _resolve_rank(system_name: str | None, rank_name: str, conn: sqlite3.Connection):
    """Returns (rank_id, rank_order, resolved_system_name). Fail-fast on ambiguity."""
    rows = conn.execute(
        "SELECT id, rank_order, system_name FROM power_ranks WHERE rank_name=?",
        (rank_name,),
    ).fetchall()
    if system_name:
        rows = [r for r in rows if r["system_name"] == system_name]
    if not rows:
        raise ProjectionError(
            f"power_ranks 未注册：{system_name or '?'}/{rank_name}（境界须先 seed）"
        )
    if len(rows) > 1:
        raise ProjectionError(f"境界名 '{rank_name}' 跨多体系，须用 'system::rank' 限定")
    return rows[0]["id"], rows[0]["rank_order"], rows[0]["system_name"]


# ── 单条迁移派发 ──────────────────────────────────────────────────────────────

def apply_state_transition(
    t: StateTransition, source_fact_id: str, conn: sqlite3.Connection
) -> str:
    """把一条 StateTransition 落到对应 *_log，回填 source_fact_id。返回新 log 行 id。
    调用方必须已在事务内（commit_canon 的 with conn:）。"""
    p = dict(t.payload or {})
    facet = p.get("facet", t.facet)
    dispatch = {
        "power": _apply_power,
        "knowledge": _apply_knowledge,
        "item": _apply_item,
        "numeric": _apply_numeric,
        "timeline": _apply_timeline,
        "gimmick_rule": _apply_gimmick_rule,
        "gimmick_use": _apply_gimmick_use,
    }
    fn = dispatch.get(facet)
    if fn is None:
        raise ProjectionError(
            f"facet '{facet}' 无硬状态投影（craft 层 foreshadow/beats/pacing 见 §05）"
        )
    return fn(t, p, source_fact_id, conn)


# ── per-facet 实现 ────────────────────────────────────────────────────────────

def _apply_power(t, p, sfid, conn) -> str:
    eid = resolve_entity_id(t.entity_id, conn)
    rank_label = p.get("rank_name") or t.to_value
    system_name = p.get("system_name")
    if "::" in rank_label:
        system_name, rank_label = rank_label.split("::", 1)
    rank_id, rank_order, system_name = _resolve_rank(system_name, rank_label, conn)
    ctype = _check_enum(
        "character_power_log.change_type",
        t.kind or p.get("change_type") or "breakthrough",
        {"breakthrough", "injury_drop", "seal", "unseal", "init"},
    )
    lid = new_id("cpl")
    conn.execute(
        "INSERT INTO character_power_log"
        "(id, entity_id, system_name, rank_id, rank_order, change_chapter, change_type,"
        " fact_id, source_fact_id)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (lid, eid, system_name, rank_id, rank_order, t.at_chapter, ctype, sfid, sfid),
    )
    return lid


def _apply_knowledge(t, p, sfid, conn) -> str:
    knower = resolve_entity_id(t.entity_id, conn)
    secret_key = p.get("secret_key") or t.to_value
    kstate = _check_enum(
        "knowledge_edges.knowledge_state",
        p.get("knowledge_state", t.kind or "knows"),
        {"knows", "suspects", "unaware", "misinformed"},
    )
    sec = p.get("secrecy_level")
    if sec is not None:
        _check_enum(
            "knowledge_edges.secrecy_level",
            sec,
            {"public", "open_secret", "secret", "top_secret"},
        )
    lid = new_id("know")
    conn.execute(
        "INSERT INTO knowledge_edges"
        "(id, knower_entity_id, secret_key, secret_fact_id, knowledge_state, learned_chapter,"
        " source, public_from_chapter, secrecy_level, fact_id, source_fact_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            lid,
            knower,
            secret_key,
            p.get("secret_fact_id"),
            kstate,
            t.at_chapter,
            p.get("source"),
            p.get("public_from_chapter"),
            sec,
            sfid,
            sfid,
        ),
    )
    return lid


def _apply_item(t, p, sfid, conn) -> str:
    item = resolve_entity_id(p.get("item_entity") or t.entity_id, conn)
    from_owner = resolve_entity_id(p["from_owner"], conn) if p.get("from_owner") else None
    to_owner = resolve_entity_id(p["to_owner"], conn) if p.get("to_owner") else None
    ctype = _check_enum(
        "item_log.change_type",
        p.get("change_type", t.kind or "acquire"),
        {"acquire", "transfer", "consume", "destroy", "craft", "lose"},
    )
    qty_delta = int(p.get("quantity_delta", 1))
    lid = new_id("ilog")
    conn.execute(
        "INSERT INTO item_log"
        "(id, item_entity_id, from_owner_id, to_owner_id, quantity_delta, change_chapter,"
        " change_type, fact_id, source_fact_id)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (lid, item, from_owner, to_owner, qty_delta, t.at_chapter, ctype, sfid, sfid),
    )
    _upsert_item_ownership(conn, item, ctype, from_owner, to_owner, qty_delta, t.at_chapter, lid)
    return lid


def _upsert_item_ownership(conn, item, ctype, from_owner, to_owner, qty_delta, chapter, log_id):
    cur = conn.execute(
        "SELECT owner_entity_id, quantity FROM item_ownership WHERE item_entity_id=?",
        (item,),
    ).fetchone()
    cur_qty = cur["quantity"] if cur else 0
    if ctype in ("acquire", "craft"):
        new_owner, new_qty = to_owner, cur_qty + abs(qty_delta)
    elif ctype == "transfer":
        new_owner, new_qty = to_owner, (cur_qty if cur else abs(qty_delta) or 1)
    else:  # consume / destroy / lose
        remain = cur_qty - abs(qty_delta)
        if remain < 0:
            raise ProjectionError(f"ITEM_DOUBLE_SPEND: {item} 余量不足（双花）")
        new_owner = (cur["owner_entity_id"] if cur else from_owner) if remain > 0 else None
        new_qty = max(remain, 0)
    conn.execute(
        "INSERT INTO item_ownership"
        "(id, item_entity_id, owner_entity_id, quantity, since_chapter, current_log_id)"
        " VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(item_entity_id) DO UPDATE SET"
        "   owner_entity_id=excluded.owner_entity_id,"
        "   quantity=excluded.quantity,"
        "   since_chapter=excluded.since_chapter,"
        "   current_log_id=excluded.current_log_id",
        (new_id("iown"), item, new_owner, new_qty, chapter, log_id),
    )


def _apply_numeric(t, p, sfid, conn) -> str:
    eid = resolve_entity_id(t.entity_id, conn) if t.entity_id else None
    mono = _check_enum(
        "numeric_facts.monotonic",
        p.get("monotonic", "none"),
        {"none", "non_decreasing", "non_increasing"},
    )
    lid = new_id("numf")
    conn.execute(
        "INSERT INTO numeric_facts"
        "(id, entity_id, metric_key, value, unit, delta_from, as_of_chapter,"
        " as_of_story_time, monotonic, fact_id, source_fact_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            lid,
            eid,
            p["metric_key"],
            float(p["value"]),
            p["unit"],
            p.get("delta_from"),
            t.at_chapter,
            p.get("as_of_story_time"),
            mono,
            sfid,
            sfid,
        ),
    )
    return lid


def _apply_timeline(t, p, sfid, conn) -> str:
    loc = resolve_location_id(p["location"], conn) if p.get("location") else None
    parts = p.get("participants")
    lid = new_id("tl")
    conn.execute(
        "INSERT INTO timeline_events"
        "(id, title, chapter, story_time_start, story_time_end, time_unit,"
        " location_id, participants, fact_id, source_fact_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            lid,
            p.get("title") or t.to_value,
            t.at_chapter,
            int(p["story_time_start"]),
            int(p["story_time_end"]),
            p.get("time_unit", "minute"),
            loc,
            json.dumps(parts, ensure_ascii=False) if parts is not None else None,
            sfid,
            sfid,
        ),
    )
    return lid


def _apply_gimmick_rule(t, p, sfid, conn) -> str:
    owner = resolve_entity_id(p["owner"], conn) if p.get("owner") else None
    lid = new_id("gim")
    conn.execute(
        "INSERT INTO gimmick_rules"
        "(id, gimmick_name, owner_entity_id, activation_cond, cost_json, cooldown_chapters,"
        " cooldown_story_time, constraint_json, valid_from_chapter, fact_id, source_fact_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(gimmick_name) DO UPDATE SET"
        "   owner_entity_id=excluded.owner_entity_id,"
        "   activation_cond=excluded.activation_cond,"
        "   cost_json=excluded.cost_json,"
        "   cooldown_chapters=excluded.cooldown_chapters,"
        "   constraint_json=excluded.constraint_json,"
        "   valid_from_chapter=excluded.valid_from_chapter,"
        "   source_fact_id=excluded.source_fact_id",
        (
            lid,
            p["gimmick_name"],
            owner,
            p.get("activation_cond"),
            json.dumps(p["cost_json"], ensure_ascii=False) if p.get("cost_json") is not None else None,
            p.get("cooldown_chapters"),
            p.get("cooldown_story_time"),
            json.dumps(p["constraint_json"], ensure_ascii=False) if p.get("constraint_json") is not None else None,
            t.at_chapter,
            sfid,
            sfid,
        ),
    )
    return lid


def _apply_gimmick_use(t, p, sfid, conn) -> str:
    user = resolve_entity_id(p.get("user_entity") or t.entity_id, conn)
    row = conn.execute(
        "SELECT id FROM gimmick_rules WHERE gimmick_name=?", (p["gimmick_name"],)
    ).fetchone()
    if row is None:
        raise ProjectionError(
            f"gimmick '{p['gimmick_name']}' 未定义（须先有 gimmick_rule fact）"
        )
    lid = new_id("gimu")
    conn.execute(
        "INSERT INTO gimmick_usage_log"
        "(id, gimmick_id, user_entity_id, use_chapter, use_story_time, outcome,"
        " paid_cost_json, fact_id, source_fact_id)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (
            lid,
            row["id"],
            user,
            t.at_chapter,
            p.get("use_story_time"),
            p.get("outcome"),
            json.dumps(p["paid_cost_json"], ensure_ascii=False) if p.get("paid_cost_json") is not None else None,
            sfid,
            sfid,
        ),
    )
    return lid


# ── retcon/revert 级联重投影（§16.8）────────────────────────────────────────────

@dataclass
class _ReplayProp:
    """轻量提案：重放只需 new/fact_type/entity/valid_from，绕过 R8 evidence 校验。"""
    op: str
    entity: str
    fact_type: str
    new: dict
    valid_from_chapter: int


def _fact_row_to_proposal(r) -> _ReplayProp:
    new = json.loads(r["detail_json"]) if r["detail_json"] else {}
    return _ReplayProp(
        op="add",
        entity=(r["entity_id"] or new.get("subject") or ""),
        fact_type=r["fact_type"],
        new=new,
        valid_from_chapter=r["valid_from_chapter"],
    )


def _canon_facts(conn, where_sql, params):
    return conn.execute(
        "SELECT id, entity_id, fact_type, object, detail_json, valid_from_chapter FROM facts "
        "WHERE status='canon' AND (valid_to_chapter IS NULL) AND "
        + where_sql
        + " ORDER BY valid_from_chapter, created_at",
        params,
    ).fetchall()


def _replay(rows, conn):
    for r in rows:
        prop = _fact_row_to_proposal(r)
        facet = (prop.new or {}).get("facet") or HARD_FACET.get(prop.fact_type)
        if facet is None:
            continue
        t = StateTransition(
            entity_id=prop.entity,
            facet=facet,
            to_value=str(
                (prop.new or {}).get("to")
                or (prop.new or {}).get("rank_name")
                or ""
            ),
            at_chapter=prop.valid_from_chapter,
            kind=(prop.new or {}).get("change_type"),
            payload={**(prop.new or {}), "facet": facet},
        )
        apply_state_transition(t, source_fact_id=r["id"], conn=conn)


def reproject_affected(entity_id: str, conn: sqlite3.Connection) -> None:
    """retcon/revert 后重建该 entity（及其牵涉物品）的派生投影。须在调用方事务内。"""
    # 1) 实体维度域：删派生行
    for tbl, key in (
        ("character_power_log", "entity_id"),
        ("knowledge_edges", "knower_entity_id"),
        ("numeric_facts", "entity_id"),
        ("gimmick_usage_log", "user_entity_id"),
        ("gimmick_rules", "owner_entity_id"),
    ):
        conn.execute(
            f"DELETE FROM {tbl} WHERE {key}=? AND source_fact_id IS NOT NULL",
            (entity_id,),
        )
    # 2) 物品域：找出 entity 牵涉的所有物品，整账重建（核验 #7）
    item_ids = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT item_entity_id FROM item_log"
            " WHERE source_fact_id IS NOT NULL"
            " AND (item_entity_id=? OR from_owner_id=? OR to_owner_id=?)",
            (entity_id, entity_id, entity_id),
        )
    }
    item_ids.add(entity_id)  # entity 本身可能就是物品
    for iid in item_ids:
        conn.execute(
            "DELETE FROM item_log WHERE item_entity_id=? AND source_fact_id IS NOT NULL",
            (iid,),
        )
        conn.execute("DELETE FROM item_ownership WHERE item_entity_id=?", (iid,))
    # 3) timeline：删来源于该 entity facts 的事件
    conn.execute(
        "DELETE FROM timeline_events WHERE source_fact_id IN"
        " (SELECT id FROM facts WHERE entity_id=?)",
        (entity_id,),
    )

    # 4) 按 canon facts 章序全量重放
    _replay(_canon_facts(conn, "entity_id=?", (entity_id,)), conn)
    for iid in item_ids:
        if iid != entity_id:
            _replay(_canon_facts(conn, "entity_id=? AND fact_type='item'", (iid,)), conn)
