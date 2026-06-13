"""卷级 Objective + KR 结算（P2#13，inkos §3.1.8）。

卷末用一次 LLM 调用判每条 KR 对照 objective 与卷情节的兑现度
（met/partial/missed + evidence），写回 volumes.key_results。

弱模型分层（P2#14）：FAST 起跑，JSON 畸形升 MID 抢救。
确定性兜底（防虚报，同伏笔防假回收）：LLM 判 met 但 evidence 空 → 降 partial；
status 非法 → 保持原值。不进 generate_chapter 热路径，仅显式触发。

KR 归一（normalize_key_results）：建卷/改卷时纯字符串列表 → 补全 id/status/evidence。
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Optional

from ..control_plane.llm.tiers import ModelTier

_VALID_STATUS = {"pending", "met", "partial", "missed"}

_KR_SYSTEM = """\
你是 NovelForge 的卷目标结算官。给定本卷的可验证目标（Objective）、若干关键结果（KR）
和本卷剧情概要，判定每条 KR 是否在本卷兑现。

对每条 KR 输出：
- status: met（完全兑现）/ partial（部分兑现）/ missed（未兑现）
- evidence: 从剧情概要中逐字摘取的支撑片段；missed 时留空

只输出 JSON 对象（不要其他说明）：
{"results": [{"id": "kr1", "status": "met", "evidence": "第8章当众揭穿..."}]}
判定从严：拿不出剧情证据的不要判 met。"""


def normalize_key_results(raw) -> list[dict]:
    """建卷/改卷入参归一：字符串列表 → [{id,text,status,evidence}]；dict 保留已有态。"""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for i, item in enumerate(raw, 1):
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append({"id": f"kr{i}", "text": text[:300],
                            "status": "pending", "evidence": ""})
        elif isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            status = item.get("status")
            out.append({
                "id": str(item.get("id") or f"kr{i}"),
                "text": text[:300],
                "status": status if status in _VALID_STATUS else "pending",
                "evidence": str(item.get("evidence") or "")[:500],
            })
    return out


def settle_volume_kr(gateway, conn: sqlite3.Connection, volume_no: int,
                     *, tier: str = "fast") -> dict:
    """结算指定卷的 KR，写回 volumes.key_results。返回报告 dict。"""
    row = conn.execute(
        "SELECT objective, key_results, rolling_summary FROM volumes WHERE volume_no=?",
        (volume_no,)).fetchone()
    if row is None:
        return {"settled": False, "reason": "volume_not_found"}
    objective = row["objective"]
    if not objective:
        return {"settled": False, "reason": "no_objective"}
    krs = _load_krs(row["key_results"])
    if not krs:
        return {"settled": False, "reason": "no_key_results"}

    summaries = "\n".join(
        f"第{r['chapter']}章：{r['summary']}" for r in conn.execute(
            "SELECT chapter, summary FROM chapter_summaries"
            " WHERE volume_no=? ORDER BY chapter", (volume_no,)).fetchall())
    material = (row["rolling_summary"] or "") + ("\n" + summaries if summaries else "")

    verdict = _call_kr_judge(gateway, tier, objective, krs, material.strip())
    if verdict is None:
        return {"settled": False, "reason": "unparseable"}

    by_id = {str(v.get("id")): v for v in (verdict.get("results") or [])
             if isinstance(v, dict)}
    counts = {"met": 0, "partial": 0, "missed": 0, "pending": 0}
    for kr in krs:
        v = by_id.get(kr["id"])
        if v is None:
            counts[kr["status"] if kr["status"] in counts else "pending"] += 1
            continue
        status = v.get("status")
        evidence = str(v.get("evidence") or "").strip()[:500]
        if status not in _VALID_STATUS:
            counts[kr["status"] if kr["status"] in counts else "pending"] += 1
            continue   # 非法判定 → 保持原值
        # 确定性兜底：判 met 但无证据 → 降 partial
        if status == "met" and not evidence:
            status = "partial"
        kr["status"] = status
        kr["evidence"] = evidence
        counts[status] += 1

    conn.execute(
        "UPDATE volumes SET key_results=? WHERE volume_no=?",
        (json.dumps(krs, ensure_ascii=False), volume_no))
    conn.commit()
    return {"settled": True, "objective": objective,
            "met": counts["met"], "partial": counts["partial"],
            "missed": counts["missed"], "results": krs}


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_krs(raw) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return normalize_key_results(data)


def _parse_kr_verdict(text: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and "results" in data else None


def _call_kr_judge(gateway, tier: str, objective: str, krs: list[dict],
                   material: str) -> Optional[dict]:
    from ..control_plane.llm.provider import Message
    try:
        mt = ModelTier(tier)
    except ValueError:
        mt = ModelTier.FAST
    kr_lines = "\n".join(f"- {k['id']}: {k['text']}" for k in krs)
    user = (f"## 本卷目标（Objective）\n{objective}\n\n"
            f"## 关键结果（KR）\n{kr_lines}\n\n"
            f"## 本卷剧情概要\n{material or '（暂无概要）'}")
    # P2#14：FAST 起跑、JSON 畸形升 MID 抢救
    result = gateway.generate_validated(
        mt, [Message(role="user", content=user)],
        parse=_parse_kr_verdict, system=_KR_SYSTEM,
        max_tokens=1024, max_tier=ModelTier.MID)
    return result.value
