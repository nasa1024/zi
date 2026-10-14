"""ChapterBatchDraftSkill：一次长输出调用连续起草多章（v22/F6 多章连写）。

只负责"写"：提示词（每章各自携带完整契约 + 目标 + 字数 + Beat Sheet，
对抗长生成中计划遵循率逐章衰减——docs/NovelForge/23 §五 Book-Writing
73.1%→50.8%）、单次 STRONG 调用、按章节标记切分并逐章做确定性校验。
逐章 check → revise → persist → settle 由 ``control_plane/batch.run_batch``
调用 ``Orchestrator.generate_chapter(predrafted_text=…)`` 完成。

输入（workspace）：
    stable_context / dynamic_context / craft_brief   起始章的上下文（与单章同源）
    writing_profile                                  起始章写作配置
    batch_chapters   [{"chapter", "goal", "contract", "beats"}]，按章号升序
输出（workspace）：
    batch_drafts     {chapter: text}  通过校验的章节正文
    batch_rejects    {chapter: reason}  missing | empty | truncated | language_mismatch | too_short
    batch_raw        原始输出
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..control_plane.llm.tiers import ModelTier
from ..control_plane.skill_base import DoDOutcome, SkillContext, SkillResult
from ..control_plane.skill_contract import DoDCheck, IOSpec, SkillContract, SkillTrigger
from ..text_metrics import count_content_units, language_matches
from ..writing_profiles import compile_system_prompt, get_project_writing_profile
from .chapter_draft_skill import _fmt_beats

_CONTRACT = SkillContract(
    name="chapter_batch_draft",
    version="1.0",
    trigger=SkillTrigger.CHAPTER_DRAFT,
    model_tier=ModelTier.STRONG,
    inputs=IOSpec(["stable_context", "batch_chapters"], "起始章上下文 + 逐章契约/节拍"),
    outputs=IOSpec(["batch_drafts", "batch_rejects"], "逐章正文 + 未通过校验的章"),
    dod=[
        DoDCheck("start_chapter_parsed", "起始章正文可用（非空、达硬下限、语言一致）"),
        DoDCheck("all_chapters_parsed", "所有请求章节均切分成功并通过校验"),
    ],
    read_scopes=["entities", "facts", "beats"],
    write_scopes=["workspace"],
    cache_prefix_keys=["project_id", "as_of_chapter"],
    description="一次长输出调用连续起草多章，按章节标记切分",
)

_SYSTEM = """\
你是 NovelForge 的连写助手，负责一次连续起草多个中文网文章节。

## 输出协议
按章节顺序逐章输出，每章正文用独立成行的标记包裹：
【第N章·开始】
（第 N 章的完整章节内容，纯中文）
【第N章·结束】

- 必须写完任务列出的每一章：章节号与任务一致，不得合并、跳过或新增章节；
- 每章严格对照该章自己的契约与 Beat Sheet，后一章从前一章结尾自然衔接；
- 标记之外不要输出任何说明、汇总或代码块；不要输出 proposals。
"""

_SYSTEM_EN = """\
You are zi's serial drafting assistant. Draft several consecutive
English-language chapters in one response, following the locked world state,
each chapter's own contract and beat sheet, and the writing profile.

Output protocol: write the chapters in order and wrap each chapter on its own
lines exactly like this:
=== CHAPTER N START ===
(the complete chapter in natural American English)
=== CHAPTER N END ===

Write every requested chapter with the requested chapter number; never merge,
skip or add chapters. Each chapter must follow its own contract and beat sheet
and continue naturally from the end of the previous chapter. Do not add
commentary, summaries, code fences or proposals outside the markers. Do not
imitate a named author or copy wording from a style reference.
"""

# 中文 / 英文章节标记（解析两种都认，容忍粗体/标题前缀与全角间隔号变体）
_ZH_MARK = re.compile(
    r"[#>*_\s]*【\s*第\s*(\d+)\s*章\s*[·・•.:：\-—_]?\s*(开始|结束)\s*】[*_]*"
)
_EN_MARK = re.compile(
    r"[#>*_\s]*={2,}\s*CHAPTER\s+(\d+)\s+(START|END)\s*={2,}[*_]*",
    re.IGNORECASE,
)
_FENCE_LINE = re.compile(r"^\s*```[\w-]*\s*$")

REJECT_REASONS = ("missing", "empty", "truncated", "language_mismatch", "too_short")


def is_english(profile: dict) -> bool:
    return str(profile.get("content_language") or "").startswith("en")


def chapter_markers(chapter: int, language: str) -> tuple[str, str]:
    """返回 (开始标记, 结束标记)。"""
    if str(language).startswith("en"):
        return f"=== CHAPTER {chapter} START ===", f"=== CHAPTER {chapter} END ==="
    return f"【第{chapter}章·开始】", f"【第{chapter}章·结束】"


def per_chapter_output_tokens(profile: dict, target_chars: int = 3000) -> int:
    """单章可见输出 token 预算（与单章起草同口径，留 1024 余量）。

    中文按 ~1.6 token/字；英文按 ``chapter_word_range`` 上限 × 1.8 token/词
    （与 ``chapter_draft_skill`` 的英文预算一致）。
    """
    if is_english(profile):
        quality = profile.get("quality_rules") or {}
        word_range = quality.get("chapter_word_range") or [1500, 2500]
        try:
            range_max = int(word_range[1])
        except (TypeError, ValueError, IndexError):
            range_max = 2500
        return int(max(1, range_max) * 1.8) + 1024
    return int(1.6 * max(1, int(target_chars or 3000))) + 1024


def hard_min_units(profile: dict) -> int:
    quality = profile.get("quality_rules") or {}
    try:
        return max(1, int(quality.get("hard_min_words") or 1000))
    except (TypeError, ValueError):
        return 1000


def _demote_headings(text: str) -> str:
    """把契约块的标题整体降一级，使其嵌套在"第 N 章"小节之下（代码块内不动）。"""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and re.match(r"^#{1,5} ", line):
            line = "#" + line
        out.append(line)
    return "\n".join(out)


def build_batch_prompt(
    workspace: dict,
    profile: dict,
    *,
    target_chars: int = 3000,
    target_words: Optional[int] = None,
) -> tuple[str, str]:
    """组装连写 user 消息，返回 ``(user_msg, stable_prefix)``。

    顺序：稳定前缀（与单章 prompt 逐字节一致）→ 当前世界状态 → 起始章 craft_brief →
    逐章小节（契约 / 目标 / 字数 / Beat Sheet）→ ``## 连写任务``。
    """
    english = is_english(profile)
    specs: list[dict] = list(workspace.get("batch_chapters") or [])
    stable = str(workspace.get("stable_context") or "")
    dynamic = str(workspace.get("dynamic_context") or "")
    context_block = "\n\n".join(
        p for p in (
            stable,
            (
                f"## Current world state\n{dynamic}" if english
                else f"## 世界状态（当前）\n{dynamic}"
            ) if dynamic else "",
        ) if p
    ) or ("(No recalled context)" if english else "(无召回上下文)")
    craft_brief = str(workspace.get("craft_brief") or "").strip()

    quality = profile.get("quality_rules") or {}
    word_range = quality.get("chapter_word_range") or [1500, 2500]
    aim_words = int(target_words or (int(word_range[0]) + int(word_range[1])) / 2)
    total = len(specs)

    sections: list[str] = []
    for i, spec in enumerate(specs, 1):
        chapter = int(spec["chapter"])
        contract = _demote_headings(str(spec.get("contract") or "").strip())
        beats = spec.get("beats") or []
        beats_str = _fmt_beats(beats) if beats else (
            "(no beats)" if english else "(无 beats)"
        )
        goal = str(spec.get("goal") or "").strip()
        if english:
            lines = [f"## Batch chapter {i}/{total} · Chapter {chapter}"]
            if contract:
                lines.append(contract)
            lines.append(
                f"### Chapter {chapter} requirements\n"
                f"Goal: {goal or 'Advance the story according to the profile.'}\n"
                f"Target length: {word_range[0]}-{word_range[1]} words "
                f"(aim for about {aim_words})"
            )
            lines.append(f"### Chapter {chapter} Beat Sheet\n{beats_str}")
        else:
            lines = [f"## 连写第 {i}/{total} 章 · 第 {chapter} 章"]
            if contract:
                lines.append(contract)
            lines.append(
                f"### 第 {chapter} 章写作要求\n"
                f"章节目标：{goal or '按情节发展'}\n"
                f"目标字数：约 {target_chars} 字"
            )
            lines.append(f"### 第 {chapter} 章 Beat Sheet\n{beats_str}")
        sections.append("\n\n".join(lines))

    chapters = [int(s["chapter"]) for s in specs]
    first, last = (chapters[0], chapters[-1]) if chapters else (0, 0)
    start_mark, end_mark = chapter_markers(first, profile.get("content_language") or "")
    if english:
        task = (
            "## Batch Writing Task\n"
            f"Draft chapters {first}-{last} ({total} chapters) consecutively, in order.\n"
            f"Wrap each chapter on its own lines, e.g. {start_mark} … {end_mark}.\n"
            "Each chapter follows its own contract and beat sheet above and picks up "
            "exactly where the previous chapter ends."
        )
    else:
        task = (
            "## 连写任务\n"
            f"连续起草第 {first}–{last} 章，共 {total} 章，按章节顺序输出。\n"
            f"每章用独立成行的标记包裹，例如 {start_mark} …… {end_mark}。\n"
            "各章分别遵守上方对应小节的契约与 Beat Sheet，后一章紧接前一章结尾。"
        )
    user_msg = (
        f"{context_block}\n\n"
        + (f"{craft_brief}\n\n" if craft_brief else "")
        + "\n\n".join(sections)
        + ("\n\n" if sections else "")
        + task
    )
    return user_msg, stable


@dataclass
class BatchParse:
    drafts: dict[int, str] = field(default_factory=dict)
    rejects: dict[int, str] = field(default_factory=dict)


def _clean_segment(text: str) -> str:
    lines = text.strip().splitlines()
    while lines and (not lines[0].strip() or _FENCE_LINE.match(lines[0])):
        lines.pop(0)
    while lines and (not lines[-1].strip() or _FENCE_LINE.match(lines[-1])):
        lines.pop()
    return "\n".join(lines).strip()


def parse_batch_output(
    text: str,
    chapters: list[int],
    *,
    language: str,
    hard_min: int,
    truncated: bool = False,
) -> BatchParse:
    """按章节标记切分连写输出并逐章校验（确定性）。

    - 中英文标记都认；容忍缺失的结束标记（截断到下一个开始标记或文末）、
      多余空白、markdown 代码围栏与乱序；
    - ``truncated=True``（输出因 max_tokens 截断）时，一直延伸到文末、没有任何
      后续标记的片段视为半截章 → ``truncated``；
    - 只返回请求范围内的章；同一章出现多次时取第一个通过校验的片段；
    - 校验：非空、未截断、``language_matches``、``count_content_units >= hard_min``。
    """
    wanted = {int(c) for c in chapters}
    marks: list[tuple[int, int, int, str]] = []   # (start, end, chapter, kind)
    for regex in (_ZH_MARK, _EN_MARK):
        for m in regex.finditer(text or ""):
            kind = "start" if m.group(2).lower() in ("开始", "start") else "end"
            marks.append((m.start(), m.end(), int(m.group(1)), kind))
    marks.sort()

    candidates: dict[int, list[tuple[str, bool]]] = {}   # chapter -> [(正文, 延伸到文末)]
    for idx, (_, body_start, chapter, kind) in enumerate(marks):
        if kind != "start" or chapter not in wanted:
            continue
        # 正文止于下一个任意标记：本章结束标记、下一章开始标记（结束标记缺失），
        # 或写错章号的结束标记（视为本章结束）；都没有则到文末。
        at_eof = idx + 1 >= len(marks)
        body_end = len(text) if at_eof else marks[idx + 1][0]
        candidates.setdefault(chapter, []).append(
            (_clean_segment(text[body_start:body_end]), at_eof),
        )

    result = BatchParse()
    for chapter in sorted(wanted):
        segments = candidates.get(chapter)
        if not segments:
            result.rejects[chapter] = "missing"
            continue
        reason = "empty"
        for segment, at_eof in segments:
            if not segment:
                continue
            if truncated and at_eof:
                reason = "truncated"
                continue
            # 先查语言：错误语言下的字/词计数没有意义
            if not language_matches(segment, language):
                reason = "language_mismatch"
                continue
            if count_content_units(segment, language) < hard_min:
                reason = "too_short"
                continue
            result.drafts[chapter] = segment
            break
        else:
            result.rejects[chapter] = reason
    return result


def provider_capabilities(gateway: Any, tier: ModelTier = ModelTier.STRONG):
    """读取 gateway 对应档位模型的 CapabilitySet；不可探测时返回 None。"""
    if gateway is None:
        return None
    try:
        return gateway._provider.capabilities(gateway.model_for(tier))
    except Exception:
        return None


class ChapterBatchDraftSkill:
    contract = _CONTRACT

    def run(self, ctx: SkillContext) -> SkillResult:
        from ..control_plane.llm.provider import CacheHint, Message

        ws = ctx.workspace
        profile = ws.get("writing_profile") or get_project_writing_profile(
            ctx.conn, chapter=ctx.target_chapter,
        )
        language = str(profile.get("content_language") or "zh-CN")
        specs: list[dict] = list(ws.get("batch_chapters") or [])
        chapters = [int(s["chapter"]) for s in specs]
        if not chapters:
            return SkillResult(
                skill_name=_CONTRACT.name, ok=False, error="no chapters requested",
            )
        target_chars = int(ctx.extra.get("draft_target_chars") or 3000)
        user_msg, stable = build_batch_prompt(
            ws, profile,
            target_chars=target_chars,
            target_words=ctx.extra.get("draft_target_words"),
        )

        per_chapter = per_chapter_output_tokens(profile, target_chars)
        caps = provider_capabilities(ctx.llm)
        max_out = len(chapters) * per_chapter
        if caps is not None:
            max_out = min(max_out, int(caps.max_tokens_out))

        resp = ctx.llm.generate(
            ModelTier.STRONG,
            [Message(role="user", content=user_msg)],
            system=compile_system_prompt(_SYSTEM, profile, english_system=_SYSTEM_EN),
            max_tokens=max(1, max_out),
            temperature=ctx.extra.get("temperature", 1.0),
            cache_hint=CacheHint(user_prefix_chars=len(stable)) if stable else None,
        )
        parsed = parse_batch_output(
            resp.text, chapters, language=language, hard_min=hard_min_units(profile),
            truncated=resp.stop_reason == "max_tokens",
        )
        ws["batch_drafts"] = parsed.drafts
        ws["batch_rejects"] = parsed.rejects
        ws["batch_raw"] = resp.text

        dod = [
            DoDOutcome(
                "start_chapter_parsed", passed=chapters[0] in parsed.drafts,
                detail=parsed.rejects.get(chapters[0], "ok"),
            ),
            DoDOutcome(
                "all_chapters_parsed", passed=not parsed.rejects,
                detail=f"parsed={sorted(parsed.drafts)}; rejected={parsed.rejects}",
            ),
        ]
        return SkillResult(
            skill_name=_CONTRACT.name,
            ok=dod[0].passed,
            payload={"drafts": parsed.drafts, "rejects": parsed.rejects},
            dod_outcomes=dod,
            usage_summary=f"in={resp.usage.input} out={resp.usage.output}",
        )
