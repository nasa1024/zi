"""CandidateJudge：章节多候选择优（M3-①）。

三级漏斗控成本：
  1. 确定性预筛（零成本）：字数 ≥1000 且 proposals 非空；全军覆没时取最长稿兜底
  2. 硬校验否决（零 LLM 成本）：对过筛候选跑确定性 validator，block 数少者优先
  3. LLM 评委（≤1 次调用）：仅当最优组仍有 ≥2 个候选时触发，pairwise 选优
     （EvolvR 实证 pairwise 优于 pointwise）

select_best() 返回报告 dict：
  {winner, scores, hard_blocks, eligible, judge_used, reason}
"""
from __future__ import annotations

import json
import re
from typing import Optional

from ..control_plane.llm.tiers import ModelTier

_MIN_DRAFT_CHARS = 1000

_JUDGE_SYSTEM = """\
你是网文主编。对比以下同一章的多个候选稿，从四个维度评估并选出最优：
① 钩子力度（章末悬念是否抓人；若本章目标含细纲契约承诺的钩子类型，以是否兑现承诺为准）② 节奏（张弛是否得当）
③ 人物声音（对话/行为是否符合人设）④ 与本章目标的契合度

输出 JSON 对象（不要其他说明）：
{"winner": 候选编号(从0起), "scores": [各候选0-10总分], "reason": "30字内理由"}
"""


def select_best(
    candidates: list[dict],
    *,
    world=None,
    chapter_goal: str = "",
    gateway=None,
    judge_tier: str = "mid",
) -> dict:
    """从候选列表选出最优。candidates 元素：{draft_text, proposals}。"""
    n = len(candidates)
    report = {
        "winner": 0,
        "scores": [None] * n,
        "hard_blocks": [0] * n,
        "eligible": [],
        "judge_used": False,
        "reason": "",
    }
    if n == 0:
        report["reason"] = "no_candidates"
        return report
    if n == 1:
        report["eligible"] = [0]
        report["reason"] = "single_candidate"
        return report

    # ── 1. 确定性预筛 ─────────────────────────────────────────────────────────
    eligible = [
        i for i, c in enumerate(candidates)
        if len(c.get("draft_text", "")) >= _MIN_DRAFT_CHARS and c.get("proposals")
    ]
    if not eligible:
        # 兜底：保证总有产出，取最长稿
        report["winner"] = max(range(n), key=lambda i: len(candidates[i].get("draft_text", "")))
        report["reason"] = "prescreen_all_failed_pick_longest"
        return report
    report["eligible"] = eligible
    if len(eligible) == 1:
        report["winner"] = eligible[0]
        report["reason"] = "prescreen_single_survivor"
        return report

    # ── 2. 硬校验否决（确定性，零 LLM 成本）──────────────────────────────────
    for i in eligible:
        report["hard_blocks"][i] = _count_hard_blocks(candidates[i]["draft_text"], world)
    min_blocks = min(report["hard_blocks"][i] for i in eligible)
    finalists = [i for i in eligible if report["hard_blocks"][i] == min_blocks]
    if len(finalists) == 1:
        report["winner"] = finalists[0]
        report["reason"] = "hard_block_veto"
        return report

    # ── 3. LLM 评委（单次调用）───────────────────────────────────────────────
    if gateway is not None:
        verdict = _llm_judge(gateway, judge_tier, chapter_goal,
                             [(i, candidates[i]["draft_text"]) for i in finalists])
        if verdict is not None:
            winner_pos, scores, reason = verdict
            if 0 <= winner_pos < len(finalists):
                report["winner"] = finalists[winner_pos]
                for pos, i in enumerate(finalists):
                    if pos < len(scores):
                        report["scores"][i] = scores[pos]
                report["judge_used"] = True
                report["reason"] = reason or "llm_judge"
                return report

    # 评委失败/缺席：取 finalists 中最长稿（信息量启发式）
    report["winner"] = max(finalists, key=lambda i: len(candidates[i].get("draft_text", "")))
    report["reason"] = "judge_unavailable_pick_longest"
    return report


def _count_hard_blocks(draft_text: str, world) -> int:
    """对单个候选跑确定性 validator，返回 block 级问题数。失败按 0 计。"""
    try:
        from ..validators.claims import extract_claims_rule
        from ..validators.items import validate_item_conservation
        from ..validators.power import validate_power_monotonicity

        claims = extract_claims_rule(draft_text)
        blocks = 0
        for validate in (validate_power_monotonicity, validate_item_conservation):
            try:
                issues = validate(claims, world)
                blocks += sum(
                    1 for i in issues
                    if (getattr(i, "severity", None) or
                        (i.get("severity") if isinstance(i, dict) else "")) == "block"
                )
            except Exception:
                pass
        return blocks
    except Exception:
        return 0


def _llm_judge(
    gateway, judge_tier: str, chapter_goal: str,
    finalists: list[tuple[int, str]],
) -> Optional[tuple[int, list, str]]:
    """单次 LLM 调用对终选候选打分选优。返回 (finalists内序号, scores, reason)。"""
    try:
        from ..control_plane.llm.provider import Message

        try:
            tier = ModelTier(judge_tier)
        except ValueError:
            tier = ModelTier.MID

        parts = [f"本章目标：{chapter_goal or '（未指定）'}\n"]
        for pos, (_, text) in enumerate(finalists):
            parts.append(f"### 候选 {pos}\n{text[:2500]}\n")
        resp = gateway.generate(
            tier,
            [Message(role="user", content="\n".join(parts))],
            system=_JUDGE_SYSTEM,
            max_tokens=2048,   # 思考型模型推理计入 max_tokens，过小会截断 JSON
        )
        return _parse_verdict(resp.text)
    except Exception:
        return None


_SCORE_SYSTEM = """\
你是网文主编。给这一章打分（0-10，可一位小数），先按四个维度分别评分，再给总分：
hook=钩子力度（章末悬念是否抓人；本章目标含钩子承诺时，验承诺是否兑现）　pacing=节奏张弛
character=人物声音（对话/行为是否符合人设）　prose=文笔表现力（含与本章目标的契合）
输出 JSON 对象（不要其他说明）：
{"score": 7.5, "dimensions": {"hook": 8.0, "pacing": 7.0, "character": 7.5, "prose": 7.0}, "reason": "30字内理由"}
"""

# WebNovelBench 式维度白名单（解析时只认这几个 key）
_SCORE_DIMENSIONS = ("hook", "pacing", "character", "prose")


def score_chapter(gateway, judge_tier: str, chapter_goal: str, draft_text: str) -> Optional[float]:
    """单稿质量总分 0-10（M5-⑦）。失败返回 None（调用方按未启用处理）。"""
    detail = score_chapter_detailed(gateway, judge_tier, chapter_goal, draft_text)
    return detail["score"] if detail else None


def score_chapter_detailed(
    gateway, judge_tier: str, chapter_goal: str, draft_text: str,
) -> Optional[dict]:
    """单稿质量分 + 维度分（钩子/节奏/人物/文笔，WebNovelBench 思路）。

    Returns {"score": float, "dimensions": {dim: float}, "reason": str}；
    失败返回 None。dimensions 可能为空 dict（评委未按格式输出维度时退化为只有总分）。
    """
    if not draft_text:
        return None
    try:
        from ..control_plane.llm.provider import Message

        try:
            tier = ModelTier(judge_tier)
        except ValueError:
            tier = ModelTier.MID
        resp = gateway.generate(
            tier,
            [Message(role="user",
                     content=f"本章目标：{chapter_goal or '（未指定）'}\n\n{draft_text[:4000]}")],
            system=_SCORE_SYSTEM,
            # 思考型模型（deepseek-v4-pro 等）的内部推理计入 max_tokens，
            # 预算过小会把 JSON 截断（实测 120 时 stop_reason=length）——上限放宽不增成本
            max_tokens=2048,
        )
        m = re.search(r"\{.*\}", resp.text, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(0))
        dims: dict[str, float] = {}
        raw_dims = data.get("dimensions")
        if isinstance(raw_dims, dict):
            for k in _SCORE_DIMENSIONS:
                try:
                    dims[k] = max(0.0, min(10.0, float(raw_dims[k])))
                except (KeyError, TypeError, ValueError):
                    continue
        try:
            score = max(0.0, min(10.0, float(data.get("score"))))
        except (TypeError, ValueError):
            # 总分缺失但维度齐全 → 取均值兜底
            if not dims:
                return None
            score = round(sum(dims.values()) / len(dims), 2)
        return {"score": score, "dimensions": dims,
                "reason": str(data.get("reason", ""))}
    except Exception:
        return None


def _parse_verdict(raw: str) -> Optional[tuple[int, list, str]]:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "winner" not in data:
        return None
    try:
        winner = int(data["winner"])
    except (TypeError, ValueError):
        return None
    scores = data.get("scores") if isinstance(data.get("scores"), list) else []
    return winner, scores, str(data.get("reason", ""))
