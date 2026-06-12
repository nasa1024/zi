"""Patch 式局部修订（M7）：revise/润色只改问题句段，不动其余内容。

动机（18 号方案 P1 遗留）：全文重写每轮输出 3000+ 字、token 贵，且容易把
没问题的段落改坏（autonovel 自承的"按下葫芦浮起瓢"）。改为 LLM 输出
锚点补丁 [{find, replace}]，程序逐条校验唯一锚定后拼接：
- find 必须在草稿中**逐字出现且唯一**，否则该补丁作废（防错位污染）
- 全部作废时调用方回退全文重写——行为永远不差于旧版
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

_PATCH_SYSTEM = """\
你是 NovelForge 修订助手。根据问题列表对草稿做**最小化修改**：只改有问题的句段，
与问题无关的内容一个字都不要动。

输出 JSON 数组，每条补丁：
{"find": "草稿原文片段", "replace": "替换后的文本"}

规则：
- find 必须**逐字复制**草稿原文（含标点），长度 10-200 字，且片段在全文中唯一
  （不唯一时多带几句前后文来锚定）
- 每个问题对应 1-2 条补丁；删除某段时 replace 给空字符串；
  需要插入时 find 锚定相邻原文，replace = 原文 + 新增内容
- 修改后须与上下文自然衔接，不破坏前后句逻辑
只输出 JSON 数组，不要其他说明。
"""


@dataclass
class PatchResult:
    text: str
    applied: int = 0
    failed: int = 0
    reasons: list[str] = field(default_factory=list)


def generate_patches(gateway, tier, *, stable_context: str, draft_text: str,
                     issues_str: str, task_label: str) -> list[dict]:
    """单次 LLM 调用产出补丁列表。失败返回 []（调用方回退全文重写）。"""
    try:
        from ..control_plane.llm.provider import CacheHint, Message
        prefix = f"{stable_context}\n\n" if stable_context else ""
        resp = gateway.generate(
            tier,
            [Message(role="user", content=(
                f"{prefix}## {task_label}\n"
                f"待修订问题：\n{issues_str}\n\n"
                f"草稿全文：\n{draft_text}"
            ))],
            system=_PATCH_SYSTEM,
            max_tokens=4096,   # 补丁输出短；思考型模型推理也计入，留足余量
            cache_hint=CacheHint(user_prefix_chars=len(stable_context)) if stable_context else None,
        )
        return parse_patches(resp.text)
    except Exception:
        return []


def parse_patches(text: str) -> list[dict]:
    """解析补丁 JSON；截断时逐步补 ] 抢救（与 proposals 解析同风格）。"""
    raw = text.strip()
    m = re.search(r"```(?:json|patches)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
    if m:
        raw = m.group(1)
    else:
        m2 = re.search(r"\[.*", raw, re.DOTALL)
        raw = m2.group(0) if m2 else ""
    if not raw:
        return []
    parsed = None
    for suffix in ("", "]", "}]", '"}]'):
        try:
            parsed = json.loads(raw + suffix)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(parsed, list):
        return []
    return [p for p in parsed if isinstance(p, dict) and "find" in p]


def apply_patches(draft_text: str, patches: list[dict]) -> PatchResult:
    """逐条应用补丁：find 唯一锚定才替换；返回应用统计。"""
    result = PatchResult(text=draft_text)
    for p in patches:
        find = str(p.get("find") or "")
        replace = str(p.get("replace") if p.get("replace") is not None else "")
        if len(find) < 4:
            result.failed += 1
            result.reasons.append("find_too_short")
            continue
        n = result.text.count(find)
        if n == 1:
            result.text = result.text.replace(find, replace)
            result.applied += 1
        elif n == 0:
            result.failed += 1
            result.reasons.append("anchor_not_found")
        else:
            result.failed += 1
            result.reasons.append("anchor_ambiguous")
    return result
