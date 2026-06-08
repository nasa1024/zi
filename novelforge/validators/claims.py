"""§10 R15 — extract_claims_rule: MVP0 确定性 claim 抽取.

机制: 正则 + 外部词表扫锚点 → 归一到 Claim。
复用 §06.3.3 高风险锚点扫描（境界词表/数值正则/地名词表/时间正则）。
召回率优先，宁多报漏，validator 自行过滤。
"""
from __future__ import annotations
import re
import sqlite3
from typing import Optional

from .types import Claim, ClaimType
from novelforge.ids import new_id


# ── 数值正则 ──────────────────────────────────────────────────────────────────
_NUMERIC_PATTERN = re.compile(
    r"(?P<value>[零一二三四五六七八九十百千万亿\d]+(?:\.\d+)?)"
    r"\s*(?P<unit>岁|块|枚|颗|粒|瓶|件|条|把|本|册|张|个|次|年|月|天|小时|分钟|"
    r"里|公里|km|米|cm|两|斤|公斤|灵石|贡献点|功德点|积分|级|层|重|阶)"
)

# ── 时间词正则 ────────────────────────────────────────────────────────────────
_TIME_REL_PATTERN = re.compile(
    r"(?P<amount>[零一二三四五六七八九十百千万\d]+)"
    r"\s*(?P<unit>年|月|天|日|小时|时辰|刻|分钟|息|瞬间|即刻)"
    r"(?P<rel>后|前|之后|之前|过后)?"
)

# ── 移动动词 ──────────────────────────────────────────────────────────────────
_MOVE_PATTERN = re.compile(
    r"(?P<subject>[一-鿿]{2,6})"
    r"(?:从|由)(?P<from_loc>[一-鿿]{2,8})"
    r"(?:到|前往|飞向|赶往|离开|抵达)(?P<to_loc>[一-鿿]{2,8})"
)


def extract_claims_rule(draft_text: str, chapter: int, conn: Optional[sqlite3.Connection] = None) -> list[Claim]:
    """Deterministic claim extraction via regex + DB wordlist.

    Args:
        draft_text: Raw chapter draft text (L0).
        chapter: Chapter number this draft belongs to.
        conn: Optional SQLite connection for loading entity/rank wordlists.

    Returns:
        List of Claim objects (may have duplicates/false positives; validator filters).
    """
    claims: list[Claim] = []

    # ── 1. 境界词 (power_level) ─────────────────────────────────────────────
    rank_names: list[str] = []
    if conn is not None:
        rows = conn.execute("SELECT rank_name FROM power_ranks ORDER BY rank_order").fetchall()
        rank_names = [r[0] for r in rows]

    if rank_names:
        # Build pattern from DB wordlist (longest first to avoid partial match)
        sorted_ranks = sorted(rank_names, key=len, reverse=True)
        rank_pattern = re.compile("|".join(re.escape(r) for r in sorted_ranks))
        for m in rank_pattern.finditer(draft_text):
            claims.append(Claim(
                claim_id=new_id("clm"),
                chapter=chapter,
                ctype=ClaimType.POWER_LEVEL,
                span=m.group(),
                span_offset=m.start(),
                payload={"rank_label": m.group(), "direction": "state"},
            ))

    # ── 2. 数值事实 (numeric) ────────────────────────────────────────────────
    for m in _NUMERIC_PATTERN.finditer(draft_text):
        val_str = m.group("value")
        unit = m.group("unit")
        try:
            val = _cn_to_float(val_str)
        except ValueError:
            continue
        claims.append(Claim(
            claim_id=new_id("clm"),
            chapter=chapter,
            ctype=ClaimType.NUMERIC,
            span=m.group(),
            span_offset=m.start(),
            payload={"key": f"numeric_{unit}", "value": val, "unit": unit, "op": "set"},
        ))

    # ── 3. 相对时间 (timeline) ───────────────────────────────────────────────
    for m in _TIME_REL_PATTERN.finditer(draft_text):
        rel = m.group("rel") or "后"
        claims.append(Claim(
            claim_id=new_id("clm"),
            chapter=chapter,
            ctype=ClaimType.TIMELINE,
            span=m.group(),
            span_offset=m.start(),
            payload={"rel_expr": m.group(), "event_key": f"ch{chapter}_event"},
        ))

    # ── 4. 移动 (location_move) ──────────────────────────────────────────────
    for m in _MOVE_PATTERN.finditer(draft_text):
        claims.append(Claim(
            claim_id=new_id("clm"),
            chapter=chapter,
            ctype=ClaimType.LOCATION_MOVE,
            subject_entity=m.group("subject"),
            span=m.group(),
            span_offset=m.start(),
            payload={
                "from_loc": m.group("from_loc"),
                "to_loc": m.group("to_loc"),
            },
        ))

    # ── 5. 实体名触及知识 (knowledge) ────────────────────────────────────────
    # MVP0: 每次提到"知道/发现/得知/告知/透露"等动词 + 信息关键词 → knowledge claim
    _KNOWLEDGE_PATTERN = re.compile(
        r"(?P<subject>[一-鿿]{2,6})(?:知道|发现|得知|察觉|看穿|识破)"
        r"(?:了|到)?(?P<info>[一-鿿]{2,16})"
    )
    for m in _KNOWLEDGE_PATTERN.finditer(draft_text):
        claims.append(Claim(
            claim_id=new_id("clm"),
            chapter=chapter,
            ctype=ClaimType.KNOWLEDGE,
            subject_entity=m.group("subject"),
            span=m.group(),
            span_offset=m.start(),
            payload={"info_key": m.group("info"), "act": "reference"},
        ))

    return claims


# ── 汉字数字转 float ──────────────────────────────────────────────────────────
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}


def _cn_to_float(s: str) -> float:
    """Convert Chinese numeral string or Arabic numeral string to float."""
    # Try Arabic first
    try:
        return float(s)
    except ValueError:
        pass
    # Convert Chinese
    result = 0
    current = 0
    for ch in s:
        if ch in _CN_DIGIT:
            current = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if unit >= 10000:
                result = (result + current) * unit
                current = 0
            else:
                current *= unit
                result += current
                current = 0
    result += current
    if result == 0:
        raise ValueError(f"Cannot convert: {s}")
    return float(result)
