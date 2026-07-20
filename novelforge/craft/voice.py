"""对话区分度校验（§05.8 INDISTINCT_DIALOGUE）。

从草稿中提取对话行，检测不同角色对话是否趋同（无角色特色）。
轻量确定性检查：对话行去除标点后取最长公共子串，相似度过高 → warn。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..text_metrics import iter_words


@dataclass
class VoiceIssue:
    detail: str
    severity: str = "warn"   # 对话区分度问题通常 warn 级别
    span: str = ""


def check_voice_distinctness(
    draft_text: str,
    entities: list[dict],
    *,
    language: str | None = None,
) -> list[VoiceIssue]:
    """检测草稿中对话区分度。entities 是 recall 里的实体列表。

    中文沿用字符集合重叠率；英文改用大小写归一后的词集合，否则
    ``character`` 和 ``chapter`` 仅因共享字母就会被误判为同声线。
    """
    lines = _extract_dialogue(draft_text, language=language)
    if len(lines) < 4:
        return []   # 对话太少，跳过

    # 计算相邻对话行的相似度（简化：词重叠率）
    issues: list[VoiceIssue] = []
    for i in range(len(lines) - 1):
        sim = _overlap_ratio(lines[i], lines[i + 1], language=language)
        if sim > 0.6:
            issues.append(VoiceIssue(
                detail=f"对话行 {i} 与 {i+1} 词汇重叠率 {sim:.0%}，角色声线可能趋同",
                span=lines[i],
            ))

    # 去重（最多报 2 条）
    return issues[:2]


def _extract_dialogue(text: str, *, language: str | None = None) -> list[str]:
    """提取中英文成对引号内的对话内容。"""
    patterns = [r'"([^"\n]+)"', r'“([^”\n]+)”', r'「([^」\n]+)」', r'『([^』\n]+)』']
    normalized = str(language or "").lower().replace("_", "-")
    if normalized.startswith("en-gb"):
        patterns.extend([r"‘([^’\n]+)’", r"(?<!\w)'([^'\n]+)'(?!\w)"])
    matches: list[tuple[int, str]] = []
    for pattern in patterns:
        matches.extend((m.start(), m.group(1).strip()) for m in re.finditer(pattern, text or ""))
    matches.sort(key=lambda item: item[0])
    return [value for _, value in matches if len(value) > 4]


def _overlap_ratio(a: str, b: str, *, language: str | None = None) -> float:
    if str(language or "").lower().replace("_", "-").startswith("en"):
        wa = {m.group(0).casefold() for m in iter_words(a)}
        wb = {m.group(0).casefold() for m in iter_words(b)}
    else:
        wa = set(a)
        wb = set(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))
