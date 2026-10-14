"""爽点兑现校验（§05.4）——防假爽点。

payoff beat 必须对应可验证的 World State 写回：
  power_up  → character_power_log 有新记录
  item_gain → item_log 有 acquire/craft 记录
  reveal    → knowledge_edges 有新知情记录
  face_slap → 不校验状态写回；但打脸的快感来自"看对方怎么输"，打脸/羞辱/comeuppance
              类 beat 必须点名在场者 witnesses（缺失 → warn，不阻断；单纯"反击"不要求）。
              正文里在场者是否真有反应由 v22/F5 扩展（craft/payoff_reactions.py）按 warn 校验。

返回 list[PayoffIssue]，severity=block 时视为假爽点。
"""
from __future__ import annotations

import re
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
    *,
    language: Optional[str] = None,
) -> list[PayoffIssue]:
    """校验所有 payoff_beat 是否有对应的 World State 变化提案（打脸类改查在场者）。"""
    issues: list[PayoffIssue] = []
    for i, beat in enumerate(beats):
        if beat.get("beat_type") != "payoff_beat":
            continue
        value_axis = beat.get("value_axis", "")
        ptype = _infer_payoff_type(value_axis, beat.get("summary", ""))
        if ptype is None:
            continue
        if ptype == "face_slap":
            # v22/F5：没有在场者就没有"看对方怎么输"。只提示，不新增 block。
            # 只对"打脸/羞辱/comeuppance…"要求在场者；单纯"反击"是交锋，不必有观众。
            from ..planning.contract_fields import normalize_witnesses
            text = f"{value_axis} {beat.get('summary', '')}".lower()
            if (any(k in text for k in _WITNESS_KEYWORDS)
                    and not normalize_witnesses(beat.get("witnesses"))):
                issues.append(PayoffIssue(
                    beat_index=i,
                    beat_type="payoff_beat",
                    payoff_type=ptype,
                    detail=_face_slap_detail(i, beat, language),
                    severity="warn",
                ))
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
    "face_slap": ["打脸", "face_slap", "羞辱", "反击",
                  "face-slap", "face slap", "humiliat", "comeuppance", "turn the tables",
                  "turns the tables"],
}

# face_slap 中需要"有人看着"的子集（不含"反击"）。
_WITNESS_KEYWORDS: tuple[str, ...] = tuple(
    k for k in _PAYOFF_KEYWORDS["face_slap"] if k != "反击"
)

_PROPOSAL_FACETS: dict[str, list[str]] = {
    "power_up":  ["power", "power_system", "power_rank"],
    "item_gain": ["item"],
    "reveal":    ["knowledge"],
    "face_slap": [],   # 不做状态写回检查：改为要求在场者（witnesses），反应由 F5 扩展校验
}

_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _face_slap_detail(index: int, beat: dict, language: Optional[str]) -> str:
    text = f"{beat.get('summary') or ''}{beat.get('value_axis') or ''}"
    english = (
        str(language).lower().replace("_", "-").startswith("en") if language
        else not _CJK_RE.search(text)
    )
    if english:
        return (
            f"beat[{index}] is a face-slap payoff but names no witnesses: the thrill comes "
            "from watching the other side lose, so list the non-protagonist characters who "
            "are present and must visibly react"
        )
    return (
        f"beat[{index}] 是打脸爽点，但没有点名在场者（witnesses）："
        "“看对方怎么输”才是快感来源，请列出在场、必须作出可见反应的非主角角色"
    )


def _infer_payoff_type(value_axis: str, summary: str) -> Optional[str]:
    text = (value_axis + " " + summary).lower()
    for ptype, kws in _PAYOFF_KEYWORDS.items():
        if any(k in text for k in kws):
            return ptype
    return None


def _has_matching_proposal(ptype: str, proposals: list[dict], chapter: int) -> bool:
    expected_facets = _PROPOSAL_FACETS.get(ptype, [])
    if not expected_facets:
        return True
    for p in proposals:
        ft = p.get("fact_type", "")
        facet = (p.get("new") or {}).get("facet", "")
        if ft in expected_facets or facet in expected_facets:
            return True
    return False
