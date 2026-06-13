"""伏笔结算（P1#6，inkos settler/hook-arbiter 同构）。

mention/advance 二分防假回收：
- mention 只记 last_mentioned_chapter，**不改 state**——「被提及 ≠ 被推进」
- advance/payoff 必须有逐字 evidence（空白归一后是终稿子串），否则降为 mention
- 新伏笔不许 LLM 直接建档：先与未解伏笔做字符 bigram Jaccard 确定性仲裁
  （≥0.5 映射为旧伏笔 mention；≤0.25 自动建档 origin='settle'；中间带拒绝）
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Optional

from ..control_plane.llm.tiers import ModelTier
from ..ids import new_id

_OPEN_STATES = ("planted", "reinforced", "misled", "overdue")
_MAP_THRESHOLD = 0.5      # ≥ 此值：映射为既有伏笔的 mention
_NEW_THRESHOLD = 0.25     # ≤ 此值：自动建档；(0.25, 0.5) 拒绝（疑似重述）

_SETTLE_SYSTEM = """\
你是 NovelForge 的伏笔结算员。对照「未解伏笔列表」审读本章终稿，判定每条伏笔在本章的遭遇：
- mention：被提及/暗示，但剧情没有实质推进
- advance：有实质推进（新线索浮现/逼近真相/相关冲突升级）
- payoff：完全兑现回收（真相揭开/承诺兑现/反转落地）
未在本章出现的伏笔不要输出。advance/payoff 必须给 evidence（逐字摘自终稿原文，10-80字）。
另外：若本章埋下了列表之外的新伏笔（明示的未解之谜/预言/反常细节），列入 new_hooks
（label ≤20字、description ≤80字、entity=关联角色名可空）；没有则空数组。
输出 JSON 对象（不要其他说明）：
{"settlements":[{"id":"fs_xxx","action":"mention|advance|payoff","evidence":"原文片段"}],
 "new_hooks":[{"label":"...","description":"...","entity":"..."}]}
"""


def settle_foreshadow(
    gateway, tier: str, conn: sqlite3.Connection, chapter: int, draft_text: str,
    *, max_new_hooks: int = 2,
) -> dict:
    """章末伏笔结算。返回报告 dict（落 detail_json["foreshadow_settle"]）。

    解析/调用失败抛异常，由调用方（orchestrator 结算块降级保护）处理。
    """
    report = {"mentions": 0, "advances": 0, "payoffs": 0,
              "new_created": [], "rejected": [], "dropped_no_evidence": 0}
    open_rows = [dict(r) for r in conn.execute(
        "SELECT f.id, f.label, f.description, f.state, f.due_chapter,"
        "       e.canonical_name AS entity_name"
        " FROM foreshadow f LEFT JOIN entities e ON e.id = f.related_entity_id"
        f" WHERE f.state IN ({','.join('?' * len(_OPEN_STATES))})"
        " ORDER BY f.planted_chapter LIMIT 20", _OPEN_STATES).fetchall()]

    data = _call_settler(gateway, tier, open_rows, chapter, draft_text)
    if data is None:
        raise RuntimeError("伏笔结算输出不可解析")

    open_ids = {r["id"] for r in open_rows}
    draft_norm = _norm_ws(draft_text)

    for s in data.get("settlements") or []:
        if not isinstance(s, dict):
            continue
        fs_id = str(s.get("id") or "")
        action = str(s.get("action") or "")
        if fs_id not in open_ids or action not in ("mention", "advance", "payoff"):
            continue
        evidence = str(s.get("evidence") or "").strip()
        # 确定性闸门（防假回收）：advance/payoff 证据必须在终稿中找到
        if action in ("advance", "payoff") and (
                not evidence or _norm_ws(evidence) not in draft_norm):
            report["dropped_no_evidence"] += 1
            action, evidence = "mention", ""
        _apply_settlement(conn, fs_id, action, chapter, evidence)
        report[action + "s"] += 1

    created = 0
    for h in data.get("new_hooks") or []:
        if not isinstance(h, dict):
            continue
        label = str(h.get("label") or "").strip()[:40]
        desc = str(h.get("description") or "").strip()[:200]
        if not label:
            continue
        best_id, best_sim = _best_match(label + " " + desc, open_rows)
        if best_sim >= _MAP_THRESHOLD and best_id:
            _apply_settlement(conn, best_id, "mention", chapter, "")
            report["mentions"] += 1
        elif best_sim <= _NEW_THRESHOLD and created < max_new_hooks:
            _create_foreshadow(conn, label, desc, chapter, h.get("entity"))
            report["new_created"].append(label)
            created += 1
        else:
            report["rejected"].append(label)
    conn.commit()
    return report


# ── LLM 调用与解析 ────────────────────────────────────────────────────────────

def _parse_settle_json(text: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _call_settler(gateway, tier: str, open_rows: list[dict],
                  chapter: int, draft_text: str) -> Optional[dict]:
    from ..control_plane.llm.provider import Message
    try:
        mt = ModelTier(tier)
    except ValueError:
        mt = ModelTier.FAST
    fs_lines = "\n".join(
        f"- id={r['id']} label={r['label']} 描述={r['description']}"
        + (f"（第{r['due_chapter']}章到期）" if r["due_chapter"] else "")
        + (f"（角色：{r['entity_name']}）" if r.get("entity_name") else "")
        for r in open_rows) or "（当前没有未解伏笔）"
    # 头 2500 + 尾 3500 字采样：回收/钩子多在章尾。不加 stable 前缀——
    # 结算 system prompt 独有，加了永远不会命中前缀缓存（同 dedup 仲裁的取舍）。
    if len(draft_text) > 6000:
        excerpt = draft_text[:2500] + "\n……（中略）……\n" + draft_text[-3500:]
    else:
        excerpt = draft_text
    # P2#14：FAST 默认、解析失败升 MID 抢救（结算不值得烧 STRONG）。
    result = gateway.generate_validated(
        mt,
        [Message(role="user", content=(
            f"## 未解伏笔列表\n{fs_lines}\n\n"
            f"## 本章（第 {chapter} 章）终稿（节选）\n{excerpt}"
        ))],
        parse=_parse_settle_json,
        system=_SETTLE_SYSTEM,
        max_tokens=2048,
        max_tier=ModelTier.MID,
    )
    return result.value


# ── 确定性写回 ────────────────────────────────────────────────────────────────

def _apply_settlement(conn, fs_id: str, action: str, chapter: int, evidence: str) -> None:
    if action == "mention":
        conn.execute(
            "UPDATE foreshadow SET last_mentioned_chapter=?, updated_at=datetime('now')"
            " WHERE id=?", (chapter, fs_id))
    elif action == "advance":
        conn.execute(
            "UPDATE foreshadow SET advance_count=advance_count+1,"
            " last_advanced_chapter=?, last_mentioned_chapter=?,"
            " state=CASE WHEN state='planted' THEN 'reinforced' ELSE state END,"
            " updated_at=datetime('now') WHERE id=?", (chapter, chapter, fs_id))
    elif action == "payoff":
        conn.execute(
            "UPDATE foreshadow SET state='paid_off', paid_off_chapter=?,"
            " last_mentioned_chapter=?, updated_at=datetime('now') WHERE id=?",
            (chapter, chapter, fs_id))
    conn.execute(
        "INSERT INTO foreshadow_log(id, foreshadow_id, chapter, action, evidence)"
        " VALUES(?,?,?,?,?)", (new_id("fsl"), fs_id, chapter, action, evidence or None))


def _create_foreshadow(conn, label: str, desc: str, chapter: int, entity) -> None:
    entity_id = None
    if entity:
        row = conn.execute(
            "SELECT id FROM entities WHERE id=? OR canonical_name=? LIMIT 1",
            (str(entity), str(entity))).fetchone()
        entity_id = row["id"] if row else None
    fs_id = new_id("fs")
    conn.execute(
        "INSERT INTO foreshadow(id, label, description, state, planted_chapter,"
        " related_entity_id, importance, origin)"
        " VALUES(?,?,?,'planted',?,?,2,'settle')",
        (fs_id, label, desc, chapter, entity_id))
    conn.execute(
        "INSERT INTO foreshadow_log(id, foreshadow_id, chapter, action, evidence)"
        " VALUES(?,?,?,'plant',NULL)", (new_id("fsl"), fs_id, chapter))


# ── 确定性仲裁（零 LLM）──────────────────────────────────────────────────────

def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _bigrams(s: str) -> set:
    s = _norm_ws(s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else ({s} if s else set())


def _similarity(a: str, b: str) -> float:
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


def _best_match(text: str, open_rows: list[dict]) -> tuple[Optional[str], float]:
    best_id, best_sim = None, 0.0
    for r in open_rows:
        sim = _similarity(text, f"{r['label']} {r['description']}")
        if sim > best_sim:
            best_id, best_sim = r["id"], sim
    return best_id, best_sim
