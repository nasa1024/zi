"""爽点兑现校验（§05.4）——防假爽点。

payoff beat 必须对应可验证的 World State 写回：
  power_up  → character_power_log 有新记录
  item_gain → item_log 有 acquire/craft 记录
  reveal    → knowledge_edges 有新知情记录
  face_slap → foreshadow 有 paid_off 状态（消费伏笔）

返回 list[PayoffIssue]，severity=block 时视为假爽点。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class PayoffIssue:
    beat_index: int
    beat_type: str
    payoff_type: str
    detail: str
    severity: str   # "block" | "warn"


def check_payoff_binding(
    beats: list[dict],
    proposals: list[dict],
    chapter: int,
    conn,
) -> list[PayoffIssue]:
    """校验所有 payoff_beat 是否有对应的 World State 变化提案。"""
    issues: list[PayoffIssue] = []
    for i, beat in enumerate(beats):
        if beat.get("beat_type") != "payoff_beat":
            continue
        value_axis = beat.get("value_axis", "")
        ptype = _infer_payoff_type(value_axis, beat.get("summary", ""))
        if ptype is None:
            continue
        if not _has_matching_proposal(ptype, proposals, chapter):
            issues.append(PayoffIssue(
                beat_index=i,
                beat_type="payoff_beat",
                payoff_type=ptype,
                detail=(
                    f"beat[{i}] value_axis={value_axis!r} 期望有 {ptype} 类提案，"
                    f"但 proposals 中未找到对应的 World State 变化"
                ),
                severity="block",
            ))
    return issues


_PAYOFF_KEYWORDS: dict[str, list[str]] = {
    "power_up":  ["突破", "升级", "境界", "power", "rank"],
    "item_gain": ["获得", "道具", "神器", "item", "acquire"],
    "reveal":    ["揭秘", "知道了", "得知", "knowledge", "reveal"],
    "face_slap": ["打脸", "打脸", "face_slap", "羞辱", "反击"],
}

_PROPOSAL_FACETS: dict[str, list[str]] = {
    "power_up":  ["power", "power_rank"],
    "item_gain": ["item"],
    "reveal":    ["knowledge"],
    "face_slap": [],   # 靠伏笔消费，暂不做程序检查
}


def _infer_payoff_type(value_axis: str, summary: str) -> Optional[str]:
    text = (value_axis + " " + summary).lower()
    for ptype, kws in _PAYOFF_KEYWORDS.items():
        if any(k in text for k in kws):
            return ptype
    return None


def _has_matching_proposal(ptype: str, proposals: list[dict], chapter: int) -> bool:
    expected_facets = _PROPOSAL_FACETS.get(ptype, [])
    if not expected_facets:
        return True   # face_slap 等暂不强制检查
    for p in proposals:
        ft = p.get("fact_type", "")
        facet = (p.get("new") or {}).get("facet", "")
        if ft in expected_facets or facet in expected_facets:
            return True
    return False
