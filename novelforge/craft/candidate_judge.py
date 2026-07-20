"""CandidateJudge：章节多候选择优（M3-①）。

五级漏斗控成本：
  1. 确定性预筛（零成本）：字数 ≥1000；空 proposals 是合法结果
  2. 世界状态硬校验（零 LLM 成本）
  3. 未来计划硬冲突（零 LLM 成本）
  4. 章节契约与情节弧推进度（零 LLM 成本）
  5. LLM 文学质量评委（≤1 次调用）
     （EvolvR 实证 pairwise 优于 pointwise）

select_best() 保留旧报告字段，并增加每个候选的 continuity_blocks、
plan_blocks、contract_score、arc_progress_score、plan_fidelity、quality_score。
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

from ..control_plane.llm.tiers import ModelTier
from ..text_metrics import count_words, language_matches
from ..writing_profiles import compile_system_prompt
from .language_quality import (
    check_english_quality,
    english_word_limits,
    profile_adherence_score,
)

_MIN_DRAFT_CHARS = 1000

_JUDGE_SYSTEM = """\
你是网文主编。对比以下同一章的多个候选稿，从五个维度评估并选出最优：
① 钩子力度（章末悬念是否抓人；若本章目标含细纲契约承诺的钩子类型，以是否兑现承诺为准）② 节奏（张弛是否得当）
③ 人物声音（对话/行为是否符合人设）④ 与本章目标的契合度
⑤ plan_fidelity（是否完成本章契约，并继续推动本卷和主线目标）

输出 JSON 对象（不要其他说明）：
{"winner": 候选编号(从0起), "scores": [各候选0-10总分], "reason": "30字内理由"}
"""

_ENGLISH_JUDGE_SYSTEM = """\
You are a web-serial fiction editor. Compare candidates for the same chapter on:
1) completion of the chapter contract and forward hook, 2) pacing, 3) character
voice, 4) fit with the chapter goal, and 5) adherence to the supplied writing
profile and long-term plan. Return JSON only:
{"winner": zero-based candidate index, "scores": [0-10 scores], "reason": "brief reason"}
"""


def select_best(
    candidates: list[dict],
    *,
    world=None,
    chapter_goal: str = "",
    chapter_contract: Optional[dict] = None,
    plan_context=None,
    gateway=None,
    judge_tier: str = "mid",
    writing_profile: Optional[dict] = None,
) -> dict:
    """从候选列表选出最优。candidates 元素：{draft_text, proposals}。"""
    n = len(candidates)
    contract_scores = [1.0] * n
    arc_progress_scores = [1.0] * n
    plan_fidelity_scores = [1.0] * n
    quality_scores = [None] * n
    english = _is_english_profile(writing_profile)
    report = {
        "winner": 0,
        "scores": [None] * n,
        "hard_blocks": [0] * n,
        "continuity_blocks": [0] * n,
        "plan_blocks": [0] * n,
        "plan_block_details": [[] for _ in range(n)],
        "contract_scores": contract_scores,
        "arc_progress_scores": arc_progress_scores,
        "plan_fidelity_scores": plan_fidelity_scores,
        "quality_scores": quality_scores,
        # Singular aliases keep already-stored/development reports readable.
        "contract_score": contract_scores,
        "arc_progress_score": arc_progress_scores,
        "plan_fidelity": plan_fidelity_scores,
        "quality_score": quality_scores,
        "eligible": [],
        "judge_used": False,
        "reason": "",
    }
    if english:
        profile = writing_profile or {}
        limits = english_word_limits(profile)
        language = str(profile.get("content_language") or profile.get("language") or "en-US")
        word_counts = [count_words(candidate.get("draft_text", "")) for candidate in candidates]
        english_findings = [
            check_english_quality(candidate.get("draft_text", ""), profile)
            for candidate in candidates
        ]
        adherence_scores = [profile_adherence_score(items) for items in english_findings]
        target_fit_scores = [
            _target_range_fit(count, limits.target_min, limits.target_max)
            for count in word_counts
        ]
        profile_fit_scores = [
            round(adherence * 0.75 + target_fit * 0.25, 4)
            for adherence, target_fit in zip(adherence_scores, target_fit_scores)
        ]
        report.update({
            "word_counts": word_counts,
            "hard_min_words": limits.hard_min,
            "target_word_range": [limits.target_min, limits.target_max],
            "target_range_matches": [
                limits.target_min <= count <= limits.target_max for count in word_counts
            ],
            "language_matches": [
                language_matches(candidate.get("draft_text", ""), language)
                for candidate in candidates
            ],
            "english_quality_findings": english_findings,
            "profile_adherence_scores": adherence_scores,
            "profile_fit_scores": profile_fit_scores,
        })
    if n == 0:
        report["reason"] = "no_candidates"
        return report
    from ..planning.scoring import evaluate_candidate_against_plan

    for i, candidate in enumerate(candidates):
        evaluation = evaluate_candidate_against_plan(
            candidate.get("draft_text", ""),
            chapter_contract=chapter_contract,
            plan_context=plan_context,
        )
        report["plan_blocks"][i] = len(evaluation["plan_blocks"])
        report["plan_block_details"][i] = evaluation["plan_blocks"]
        report["contract_scores"][i] = evaluation["contract_score"]
        report["arc_progress_scores"][i] = evaluation["arc_progress_score"]
        report["plan_fidelity_scores"][i] = evaluation["plan_fidelity"]

    if n == 1:
        report["continuity_blocks"][0] = _count_hard_blocks(
            candidates[0].get("draft_text", ""), world
        )
        report["hard_blocks"] = list(report["continuity_blocks"])
        report["eligible"] = [0]
        report["reason"] = "single_candidate"
        return report

    # ── 1. 确定性预筛 ─────────────────────────────────────────────────────────
    if english:
        eligible = [
            i for i in range(n)
            if report["language_matches"][i]
            and report["word_counts"][i] >= report["hard_min_words"]
        ]
        # Target range is a profile-fit signal, not a hard veto.  Every draft
        # that clears language + hard minimum must reach continuity and plan
        # checks before range proximity can break a tie.
    else:
        eligible = [
            i for i, c in enumerate(candidates)
            if len(c.get("draft_text", "")) >= _MIN_DRAFT_CHARS
        ]
    if not eligible:
        if english:
            # Do not reward an arbitrarily long English draft.  Prefer matching
            # language, progress toward the hard floor, range proximity and
            # deterministic profile adherence; stable index breaks exact ties.
            report["winner"] = max(
                range(n),
                key=lambda i: _failed_english_rank(report, i),
            )
            report["reason"] = "english_prescreen_all_failed_best_fit"
        else:
            # 中文兼容兜底：保持既有最长稿行为。
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
        report["continuity_blocks"][i] = _count_hard_blocks(
            candidates[i]["draft_text"], world
        )
    report["hard_blocks"] = list(report["continuity_blocks"])
    min_blocks = min(report["continuity_blocks"][i] for i in eligible)
    finalists = [i for i in eligible if report["continuity_blocks"][i] == min_blocks]
    if len(finalists) == 1:
        report["winner"] = finalists[0]
        report["reason"] = "hard_block_veto"
        return report

    # ── 3. 未来计划硬冲突 ──────────────────────────────────────────────────
    min_plan_blocks = min(report["plan_blocks"][i] for i in finalists)
    finalists = [i for i in finalists if report["plan_blocks"][i] == min_plan_blocks]
    if len(finalists) == 1:
        report["winner"] = finalists[0]
        report["reason"] = "plan_block_veto"
        return report

    # ── 4. 完成章节契约并推进长期路线 ──────────────────────────────────────
    best_fidelity = max(report["plan_fidelity_scores"][i] for i in finalists)
    finalists = [
        i for i in finalists
        if abs(report["plan_fidelity_scores"][i] - best_fidelity) <= 1e-6
    ]
    if len(finalists) == 1:
        report["winner"] = finalists[0]
        report["reason"] = "plan_fidelity_veto"
        return report

    # ── 4b. 英文 profile 遵守度 + 目标区间贴合度（确定性）─────────────────
    if english:
        best_profile_fit = max(report["profile_fit_scores"][i] for i in finalists)
        finalists = [
            i for i in finalists
            if abs(report["profile_fit_scores"][i] - best_profile_fit) <= 1e-6
        ]
        if len(finalists) == 1:
            report["winner"] = finalists[0]
            report["reason"] = "profile_adherence_veto"
            return report

    # ── 5. LLM 文学质量评委（单次调用）─────────────────────────────────────
    if gateway is not None:
        verdict = _llm_judge(
            gateway,
            judge_tier,
            chapter_goal,
            [(i, candidates[i]["draft_text"]) for i in finalists],
            chapter_contract=chapter_contract,
            plan_context=plan_context,
            writing_profile=writing_profile,
        )
        if verdict is not None:
            winner_pos, scores, reason = verdict
            if 0 <= winner_pos < len(finalists):
                report["winner"] = finalists[winner_pos]
                for pos, i in enumerate(finalists):
                    if pos < len(scores):
                        report["scores"][i] = scores[pos]
                        report["quality_scores"][i] = scores[pos]
                report["judge_used"] = True
                report["reason"] = reason or "llm_judge"
                return report

    if english:
        # All deterministic criteria are tied.  Stable source order is
        # reproducible and does not reintroduce "longest draft wins".
        report["winner"] = min(finalists)
        report["reason"] = "judge_unavailable_profile_fit"
    else:
        # 中文默认行为保持不变。
        report["winner"] = max(finalists, key=lambda i: len(candidates[i].get("draft_text", "")))
        report["reason"] = "judge_unavailable_pick_longest"
    return report


def _count_hard_blocks(draft_text: str, world) -> int:
    """对单个候选跑确定性 validator，返回 critical 级问题数。失败按 0 计。

    P2#12 修复：原 import 名错（validate_item_conservation）+ extract_claims_rule
    缺参 + 数 'block'（Issue 域是 critical/major/...）——该否决层从未生效过。
    主语绑定复用 continuity_check 的接线逻辑，保证两处判定口径一致。
    """
    if world is None:
        return 0
    try:
        from ..skills.continuity_check_skill import _bind_power_subjects
        from ..validators import (
            extract_claims_rule, refine_knowledge_claims,
            validate_item_inventory, validate_knowledge_edges,
            validate_power_monotonicity,
        )

        conn = world._conn
        chapter = world.as_of + 1
        raw = extract_claims_rule(draft_text, chapter, conn)
        claims = (refine_knowledge_claims(raw, conn)
                  + _bind_power_subjects(
                      [c for c in raw
                       if getattr(c.ctype, "value", c.ctype) == "power_level"],
                      draft_text, conn)
                  + [c for c in raw
                     if getattr(c.ctype, "value", c.ctype) not in ("power_level", "knowledge")])
        blocks = 0
        for validate in (validate_power_monotonicity, validate_item_inventory,
                         validate_knowledge_edges):
            try:
                blocks += sum(1 for i in validate(claims, world, conn)
                              if i.severity == "critical")
            except Exception:
                pass
        return blocks
    except Exception:
        return 0


def _llm_judge(
    gateway, judge_tier: str, chapter_goal: str,
    finalists: list[tuple[int, str]],
    *,
    chapter_contract: Optional[dict] = None,
    plan_context=None,
    writing_profile: Optional[dict] = None,
) -> Optional[tuple[int, list, str]]:
    """单次 LLM 调用对终选候选打分选优。返回 (finalists内序号, scores, reason)。"""
    try:
        from ..control_plane.llm.provider import Message

        try:
            tier = ModelTier(judge_tier)
        except ValueError:
            tier = ModelTier.MID

        contract_text = ""
        if plan_context is not None and hasattr(plan_context, "render_contract_for_draft"):
            contract_text = plan_context.render_contract_for_draft(
                str((writing_profile or {}).get("content_language") or "zh-CN")
            )
        elif chapter_contract:
            contract_text = json.dumps(chapter_contract, ensure_ascii=False)
        if _is_english_profile(writing_profile):
            profile_summary = json.dumps(writing_profile or {}, ensure_ascii=False)
            parts = [f"Chapter goal: {chapter_goal or '(not specified)'}\n"]
            parts.append(f"## Writing profile\n{profile_summary[:5000]}\n")
            if contract_text:
                parts.append(f"## Chapter contract\n{contract_text[:5000]}\n")
            for pos, (_, text) in enumerate(finalists):
                parts.append(f"### Candidate {pos}\n{text[:2500]}\n")
            system = _ENGLISH_JUDGE_SYSTEM
        else:
            parts = [f"本章目标：{chapter_goal or '（未指定）'}\n"]
            if contract_text:
                parts.append(f"## 计划契约\n{contract_text[:5000]}\n")
            for pos, (_, text) in enumerate(finalists):
                parts.append(f"### 候选 {pos}\n{text[:2500]}\n")
            system = _JUDGE_SYSTEM
        resp = gateway.generate(
            tier,
            [Message(role="user", content="\n".join(parts))],
            system=system,
            max_tokens=2048,   # 思考型模型推理计入 max_tokens，过小会截断 JSON
        )
        return _parse_verdict(resp.text)
    except Exception:
        return None


def _is_english_profile(profile: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(profile, Mapping):
        return False
    language = str(
        profile.get("content_language") or profile.get("language") or ""
    ).lower().replace("_", "-")
    return language.startswith("en")


def _target_range_fit(word_count: int, target_min: int, target_max: int) -> float:
    if target_min <= word_count <= target_max:
        return 1.0
    if word_count < target_min:
        return round(max(0.0, word_count / max(1, target_min)), 4)
    return round(max(0.0, 1.0 - (word_count - target_max) / max(1, target_max)), 4)


def _failed_english_rank(report: Mapping[str, Any], index: int) -> tuple:
    hard_min = max(1, int(report["hard_min_words"]))
    hard_progress = min(1.0, report["word_counts"][index] / hard_min)
    return (
        1 if report["language_matches"][index] else 0,
        round(hard_progress, 4),
        report["profile_fit_scores"][index],
        report["plan_fidelity_scores"][index],
        -index,
    )


_SCORE_SYSTEM = """\
你是网文主编。给这一章打分（0-10，可一位小数），先按四个维度分别评分，再给总分：
hook=钩子力度（章末悬念是否抓人；本章目标含钩子承诺时，验承诺是否兑现）　pacing=节奏张弛
character=人物声音（对话/行为是否符合人设）　prose=文笔表现力（含与本章目标的契合）
输出 JSON 对象（不要其他说明）：
{"score": 7.5, "dimensions": {"hook": 8.0, "pacing": 7.0, "character": 7.5, "prose": 7.0}, "reason": "30字内理由"}
"""

_SCORE_SYSTEM_EN = """\
You are a fiction editor scoring one chapter from 0 to 10. Score four dimensions
before the overall score: hook (or the profile-appropriate chapter landing),
pacing, character voice, and prose/goal fit. Judge the chapter against the locked
writing profile; do not impose progression-fantasy beats on every genre. Return
JSON only:
{"score": 7.5, "dimensions": {"hook": 8.0, "pacing": 7.0, "character": 7.5, "prose": 7.0}, "reason": "brief reason"}
"""

# WebNovelBench 式维度白名单（解析时只认这几个 key）
_SCORE_DIMENSIONS = ("hook", "pacing", "character", "prose")


def score_chapter(
    gateway, judge_tier: str, chapter_goal: str, draft_text: str,
    writing_profile: Optional[dict] = None,
) -> Optional[float]:
    """单稿质量总分 0-10（M5-⑦）。失败返回 None（调用方按未启用处理）。"""
    detail = score_chapter_detailed(
        gateway, judge_tier, chapter_goal, draft_text, writing_profile,
    )
    return detail["score"] if detail else None


def score_chapter_detailed(
    gateway, judge_tier: str, chapter_goal: str, draft_text: str,
    writing_profile: Optional[dict] = None,
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
        english = _is_english_profile(writing_profile)
        system = (
            compile_system_prompt(
                _SCORE_SYSTEM,
                writing_profile or {},
                english_system=_SCORE_SYSTEM_EN,
            )
            if english else _SCORE_SYSTEM
        )
        goal_label = "Chapter goal: " if english else "本章目标："
        missing_goal = "(not specified)" if english else "（未指定）"
        resp = gateway.generate(
            tier,
            [Message(role="user",
                     content=f"{goal_label}{chapter_goal or missing_goal}\n\n{draft_text[:4000]}")],
            system=system,
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
