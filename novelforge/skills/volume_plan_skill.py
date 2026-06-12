"""VolumePlanSkill：卷级批量预规划（M4-④）。

输入（workspace）：
  volume_brief    {volume_no, title, synopsis, rolling_summary}
  plan_from / plan_to   规划章节范围（单次 ≤10 章，防截断）
  foreshadow_due  待回收伏笔列表 [{label, due_chapter}]
  stable_context  慢变设定（可选，与生成管线共享前缀）

输出（workspace）：
  chapter_plans   [{chapter, title, goal, hook_text, beats:[{beat_type, summary, value_axis}]}]

落库由 API 层负责（只覆盖 status='planned' 的章，保护既成事实）。
"""
from __future__ import annotations

import json
import re

from ..control_plane.llm.tiers import ModelTier
from ..control_plane.skill_base import DoDOutcome, SkillContext, SkillResult
from ..control_plane.skill_contract import (
    DoDCheck, IOSpec, SkillContract, SkillTrigger,
)

_CONTRACT = SkillContract(
    name="volume_plan",
    version="1.0",
    trigger=SkillTrigger.MANUAL,
    model_tier=ModelTier.STRONG,
    inputs=IOSpec(["volume_brief", "plan_from", "plan_to", "foreshadow_due"],
                  "卷概要 + 规划范围 + 到期伏笔"),
    outputs=IOSpec(["chapter_plans"], "逐章章节卡（title/goal/hook + planned beats）"),
    dod=[
        DoDCheck("has_plans", "至少产出 1 章规划"),
        DoDCheck("hooks_present", "每章 hook_text 非空"),
    ],
    read_scopes=["volumes", "foreshadow", "chapter_summaries"],
    write_scopes=["workspace"],
    description="按卷大纲批量生成逐章细纲（章节卡 + planned beats），网文工艺字段显式化",
)

# 钩子枚举两处同步：菜单字符串与 craft/hooks.py 的枚举保持一致（枚举变更时同改）
_SYSTEM = """\
你是 NovelForge 的卷规划师，为网文一卷生成逐章细纲（章节卡）。

## 输出格式
只输出一个 JSON 代码块：
```plans
[
  {
    "chapter": 12,
    "title": "章节名",
    "goal": "本章目标：必须含明确冲突或爽点",
    "hook_text": "章末悬念一句话（必填）",
    "target_emotion": "本章目标情绪词（如 紧张/扬眉吐气/悲怆）",
    "opening_hook_type": "章首钩子，7 式选一: suspense(悬念)/conflict(冲突)/dialogue(对话切入)/action(动作)/anomaly(反常)/crisis(危机)/flashback(倒叙)",
    "hook_type": "章尾钩子，13 式选一: cliffhanger(命悬一线)/reversal(反转)/reveal(揭秘)/new_threat(新威胁)/mystery(新谜团)/promise(承诺约战)/arrival(神秘登场)/decision(重大抉择)/countdown(倒计时)/loss(失去代价)/power_tease(力量预告)/relationship(关系变化)/humiliation(受辱蓄势)",
    "expectation_score": 4,
    "beats": [
      {"beat_type": "setup|turn|tension_point|payoff_beat|hook", "summary": "...", "value_axis": "如 安全→危机"}
    ]
  }
]
```

## 规划规则（网文工艺）
- 每章 goal 必须包含明确的冲突或爽点，禁止纯过渡章
- hook_text 必填：让读者点开下一章的钩子
- 相邻两章 hook_type 不得相同（钩子同质化让读者疲劳）；expectation_score 是章尾钩子期待度 1-5
- beats 每章 3-5 条，最后一条必须是 hook 类型
- 若给出"待回收伏笔"，必须在其到期章或之前安排回收（payoff_beat）
- 章节号严格按给定范围连续编号，不重不漏
只输出 plans 代码块，不要其他说明。
"""


class VolumePlanSkill:
    contract = _CONTRACT

    def run(self, ctx: SkillContext) -> SkillResult:
        brief: dict = ctx.workspace.get("volume_brief", {})
        plan_from: int = ctx.workspace.get("plan_from", 1)
        plan_to: int = ctx.workspace.get("plan_to", plan_from)
        foreshadow_due: list[dict] = ctx.workspace.get("foreshadow_due", [])
        stable: str = ctx.workspace.get("stable_context", "")

        parts = []
        if stable:
            parts.append(stable)
        parts.append(
            f"## 本卷\n《{brief.get('title', '未命名')}》（第 {brief.get('volume_no', '?')} 卷）\n"
            f"卷大纲：{brief.get('synopsis') or '（无）'}"
        )
        if brief.get("rolling_summary"):
            parts.append(f"## 本卷至今\n{brief['rolling_summary']}")
        if foreshadow_due:
            fs = "、".join(f"{f.get('label')}（第{f.get('due_chapter')}章到期）" for f in foreshadow_due)
            parts.append(f"## 待回收伏笔\n{fs}")
        parts.append(f"## 规划范围\n第 {plan_from} 章 至 第 {plan_to} 章（共 {plan_to - plan_from + 1} 章）")
        user_msg = "\n\n".join(parts)

        from ..control_plane.llm.provider import Message
        resp = ctx.llm.generate(
            ModelTier.STRONG,
            [Message(role="user", content=user_msg)],
            system=_SYSTEM,
            max_tokens=8192,
        )

        plans = _parse_plans(resp.text, plan_from, plan_to)
        ctx.workspace["chapter_plans"] = plans

        dod = [
            DoDOutcome("has_plans", passed=bool(plans), detail=f"plans={len(plans)}"),
            DoDOutcome("hooks_present",
                       passed=bool(plans) and all(p.get("hook_text") for p in plans),
                       detail="逐章 hook_text 校验"),
        ]
        return SkillResult(
            skill_name="volume_plan",
            ok=bool(plans),
            payload={"chapter_plans": plans},
            dod_outcomes=dod,
            usage_summary=f"in={resp.usage.input} out={resp.usage.output}",
        )


_VALID_BEAT_TYPES = {"setup", "turn", "payoff_beat", "tension_point", "hook"}


def _parse_plans(text: str, plan_from: int, plan_to: int) -> list[dict]:
    """提取 ```plans 块并解析；截断时逐步补 ] 抢救；过滤越界章节。"""
    raw = ""
    m = re.search(r"```plans\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        raw = m.group(1)
    else:
        m2 = re.search(r"```plans\s*(.*)", text, re.DOTALL)
        if m2:
            raw = m2.group(1)
        else:
            m3 = re.search(r"\[.*\]", text, re.DOTALL)
            raw = m3.group(0) if m3 else ""
    raw = raw.strip()
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

    from ..craft.hooks import normalize_hook_type

    plans: list[dict] = []
    for p in parsed:
        if not isinstance(p, dict):
            continue
        try:
            ch = int(p.get("chapter"))
        except (TypeError, ValueError):
            continue
        if not (plan_from <= ch <= plan_to):
            continue
        beats = []
        for i, b in enumerate(p.get("beats") or []):
            if not isinstance(b, dict):
                continue
            bt = b.get("beat_type")
            if bt not in _VALID_BEAT_TYPES:
                continue
            beats.append({
                "seq": i + 1,
                "beat_type": bt,
                "summary": str(b.get("summary", ""))[:500],
                "value_axis": str(b.get("value_axis", ""))[:100] or None,
            })
        exp = p.get("expectation_score")
        try:
            exp = max(1, min(5, int(exp))) if exp is not None else None
        except (TypeError, ValueError):
            exp = None
        plans.append({
            "chapter": ch,
            "title": str(p.get("title", ""))[:100],
            "goal": str(p.get("goal", ""))[:1000],
            "hook_text": str(p.get("hook_text", ""))[:300],
            "target_emotion": (str(p.get("target_emotion"))[:50]
                               if p.get("target_emotion") else None),
            "opening_hook_type": normalize_hook_type(p.get("opening_hook_type"), "opening"),
            "hook_type": normalize_hook_type(p.get("hook_type"), "ending"),
            "expectation_score": exp,
            "beats": beats,
        })
    plans.sort(key=lambda x: x["chapter"])
    return plans
