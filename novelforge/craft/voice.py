"""对话区分度校验（§05.8 INDISTINCT_DIALOGUE）。

从草稿中提取对话行，检测不同角色对话是否趋同（无角色特色）。
轻量确定性检查：对话行去除标点后取最长公共子串，相似度过高 → warn。
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class VoiceIssue:
    detail: str
    severity: str = "warn"   # 对话区分度问题通常 warn 级别


def check_voice_distinctness(draft_text: str, entities: list[dict]) -> list[VoiceIssue]:
    """检测草稿中对话区分度。entities 是 recall 里的实体列表。"""
    lines = _extract_dialogue(draft_text)
    if len(lines) < 4:
        return []   # 对话太少，跳过

    # 计算相邻对话行的相似度（简化：词重叠率）
    issues: list[VoiceIssue] = []
    for i in range(len(lines) - 1):
        sim = _overlap_ratio(lines[i], lines[i + 1])
        if sim > 0.6:
            issues.append(VoiceIssue(
                detail=f"对话行 {i} 与 {i+1} 词汇重叠率 {sim:.0%}，角色声线可能趋同",
            ))

    # 去重（最多报 2 条）
    return issues[:2]


def _extract_dialogue(text: str) -> list[str]:
    """提取引号内的对话内容。"""
    lines = re.findall(r'[""「](.*?)[""」]', text)
    return [l.strip() for l in lines if len(l.strip()) > 4]


def _overlap_ratio(a: str, b: str) -> float:
    wa = set(a)
    wb = set(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))
