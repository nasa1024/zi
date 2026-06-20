"""SettingGenSkill：正向生成情节设定候选（建项目前，无 project DB）。

全仓库首个「不读 ctx.conn」的 Skill —— run() 严格不访问数据库（单测以 _ConnSpy 断言）。
一次产出 N 个彼此不同的候选；支持基于已锁定字段的条件生成与单字段 reroll。

输入（workspace）：
  genre/name/hints   用户已填的题材/书名/脑洞
  n                  候选数（1~5；给 field 时强制 1）
  locked             已锁定字段值 dict（条件生成前提）
  locked_fields      显式锁定的 key 列表
  field              单字段 reroll 目标 key（非空 = 只重生该字段）
  avoid              针对生成目标的历史值，去重用

输出（workspace）：
  candidates         list[dict]（PlotSetting 形状）
  tier_used          实际产出档位
"""
from __future__ import annotations

import json
import re

from ..app.models import GENRE_GENERATABLE, GENRE_VALUES
from ..control_plane.llm.tiers import ModelTier
from ..control_plane.skill_base import DoDOutcome, SkillContext, SkillResult
from ..control_plane.skill_contract import (
    DoDCheck, IOSpec, SkillContract, SkillTrigger,
)

# PlotSetting 文本字段（power_ranks 单独处理为有序列表）
_TEXT_FIELDS = (
    "premise", "world_setting", "power_system", "protagonist",
    "main_conflict", "opening_hook", "target_audience", "tone",
)
_REQUIRED_FIELDS = ("premise", "protagonist", "main_conflict")
_MAX_LEN = 1000
_MAX_RANKS = 30


_CONTRACT = SkillContract(
    name="setting_gen",
    version="1.0",
    trigger=SkillTrigger.MANUAL,
    model_tier=ModelTier.MID,
    inputs=IOSpec(
        ["name", "genre", "hints", "n", "locked", "locked_fields", "field", "avoid"],
        "用户已填字段 + 锁定前提 + 候选数 + reroll 目标",
    ),
    outputs=IOSpec(["candidates", "tier_used"], "N 份 PlotSetting 候选 + 实际档位"),
    dod=[
        DoDCheck("has_candidates", "至少产出 1 个候选"),
        DoDCheck("genre_valid", "每个候选 genre 在白名单内"),
        DoDCheck("min_fields", "每个候选 premise/protagonist/main_conflict 非空"),
    ],
    read_scopes=["workspace"],     # 文档性声明：本 skill 不读 DB（运行时不强制）
    write_scopes=["workspace"],
    cache_prefix_keys=["locked", "genre"],
    description="据用户输入正向生成 N 个彼此不同的情节设定候选（建项目前，无 DB）",
)


_SYSTEM = """\
你是 NovelForge 的网文设定策划。根据用户输入生成「情节设定」候选。

## 输出格式
只输出一个 JSON 代码块：
```settings
[
  {
    "genre": "题材英文 key",
    "premise": "一句话设定（必填）",
    "world_setting": "世界观背景",
    "power_system": "力量体系名",
    "power_ranks": ["阶梯1(最弱)", "阶梯2", "更强..."],
    "protagonist": "主角设定（必填）",
    "main_conflict": "主线冲突（必填）",
    "opening_hook": "开篇钩子",
    "target_audience": "目标读者",
    "tone": "基调"
  }
]
```

## 规则
- genre 只能取其一：%s
- premise / protagonist / main_conflict 必填，不得为空
- 生成多个候选时，各候选的 power_system 名与 main_conflict 必须彼此不同（拒绝同质化）
- power_ranks 按由弱到强排序
只输出 settings 代码块，不要任何额外说明。
""" % "/".join(GENRE_GENERATABLE)


class SettingGenSkill:
    contract = _CONTRACT

    def run(self, ctx: SkillContext) -> SkillResult:
        # 关键自律：全程不引用 ctx.conn
        ws = ctx.workspace
        n = max(1, min(5, int(ws.get("n") or 3)))
        field = ws.get("field") or None
        if field:
            n = 1
        locked = ws.get("locked") or None
        avoid = ws.get("avoid") or []

        locked_fields = ws.get("locked_fields") or []

        stable, dynamic = _build_prompt(ws, n, field, locked, avoid)
        user_msg = f"{stable}\n\n{dynamic}" if stable else dynamic

        from ..control_plane.llm.provider import CacheHint, Message
        result = ctx.llm.generate_validated(
            ModelTier.MID,
            [Message(role="user", content=user_msg)],
            parse=lambda txt: _parse_candidates(
                txt, n=n, field=field, locked=locked, avoid=avoid,
                locked_fields=locked_fields, require_n=not field),
            system=_SYSTEM,
            max_tokens=4096,
            temperature=1.0,
            cache_hint=CacheHint(user_prefix_chars=len(stable)) if stable else None,
            max_tier=ModelTier.MID,   # 探索阶段单档；升档收益仅在 anthropic provider 成立
        )

        fallback_used = not result.value
        candidates = result.value or [_fallback_candidate(ws)]
        if fallback_used:
            ctx.workspace["degraded"] = True
            ctx.workspace["fallback_reason"] = "parse_failed_or_insufficient_candidates"
        ctx.workspace["candidates"] = candidates
        ctx.workspace["tier_used"] = result.tier_used

        dod = [
            DoDOutcome("has_candidates", passed=bool(candidates),
                       detail=f"n={len(candidates)}"),
            DoDOutcome("genre_valid",
                       passed=all(c.get("genre") in GENRE_VALUES for c in candidates)),
            DoDOutcome("min_fields",
                       passed=all(all(c.get(f) for f in _REQUIRED_FIELDS)
                                  for c in candidates)),
        ]
        return SkillResult(
            skill_name="setting_gen",
            ok=bool(candidates),
            payload={"candidates": candidates},
            dod_outcomes=dod,
            usage_summary=f"tier={result.tier_used} esc={result.escalated}",
        )


# ── prompt 构造 ────────────────────────────────────────────────────────────────

def _build_prompt(ws: dict, n: int, field, locked, avoid) -> tuple[str, str]:
    """返回 (stable_prefix, dynamic_task)。stable 为可缓存前缀。"""
    stable_parts: list[str] = []
    if locked:
        kept = {k: v for k, v in locked.items() if v}
        if kept:
            stable_parts.append("## 已确定的设定（保持一致）\n" + json.dumps(
                kept, ensure_ascii=False))
    stable = "\n\n".join(stable_parts)

    dyn: list[str] = []
    genre = ws.get("genre")
    name = ws.get("name")
    hints = ws.get("hints")
    if name:
        dyn.append(f"书名：{name}")
    if genre:
        dyn.append(f"题材（genre）：{genre}")
    if hints:
        dyn.append(f"脑洞/要求：{hints}")

    if field:
        dyn.append(
            f"## 任务\n只重新生成字段「{field}」一项，其余字段保持不变。"
            f"输出 1 个候选，仅需该字段有意义。"
        )
        if avoid:
            dyn.append("不得与以下已出现的值重复：" + "；".join(str(a) for a in avoid))
    else:
        dyn.append(f"## 任务\n生成 {n} 个彼此不同的完整设定候选。")
        if avoid:
            dyn.append("避免与以下已出现的设定雷同：" + "；".join(str(a) for a in avoid))
    return stable, "\n".join(dyn)


# ── 解析 ───────────────────────────────────────────────────────────────────────

def _extract_block(text: str) -> str:
    m = re.search(r"```settings\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"```settings\s*(.*)", text, re.DOTALL)
    if m2:
        return m2.group(1).strip()
    m3 = re.search(r"\[.*\]", text, re.DOTALL)
    return m3.group(0).strip() if m3 else ""


def _load_json_array(raw: str):
    """逐步补收尾符号抢救被截断的 JSON 数组（抄 volume_plan_skill._parse_plans）。"""
    if not raw:
        return None
    for suffix in ("", "]", "}]", '"}]', '"}}]'):
        try:
            parsed = json.loads(raw + suffix)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _norm_ranks(val) -> list[str]:
    if not isinstance(val, list):
        return []
    out: list[str] = []
    for r in val[:_MAX_RANKS]:
        s = str(r).strip()[:50]
        if s:
            out.append(s)
    return out


def _normalize(raw: dict) -> dict | None:
    """规范化一个候选 dict；genre 非法或必填字段缺失返回 None。"""
    if not isinstance(raw, dict):
        return None
    genre = str(raw.get("genre") or "").strip()
    if genre not in GENRE_VALUES:
        return None
    out: dict = {"genre": genre, "power_ranks": _norm_ranks(raw.get("power_ranks"))}
    for f in _TEXT_FIELDS:
        v = raw.get(f)
        out[f] = str(v).strip()[:_MAX_LEN] if v else None
    if not all(out.get(f) for f in _REQUIRED_FIELDS):
        return None
    return out


def _parse_candidates(
    text, n, field=None, locked=None, avoid=None, locked_fields=None,
    require_n: bool = False,
):
    """解析 LLM 输出为候选列表。失败/同质/不达标返回 None（触发升档/兜底）。"""
    avoid = avoid or []
    locked_fields = set(locked_fields or [])
    parsed = _load_json_array(_extract_block(text))
    if not parsed:
        return None

    # ── 单字段 reroll：只取目标字段，与 locked 合并 ──
    if field:
        base = dict(locked or {})
        new_val = None
        for item in parsed:
            if isinstance(item, dict) and item.get(field) not in (None, ""):
                new_val = item.get(field)
                break
        if new_val is None:
            return None
        if field == "power_ranks":
            new_val = _norm_ranks(new_val)
            if not new_val:
                return None
        else:
            new_val = str(new_val).strip()[:_MAX_LEN]
            if not new_val or new_val in [str(a) for a in avoid]:
                return None
        base[field] = new_val
        merged = _normalize(base)
        return [merged] if merged else None

    # ── 多候选：规范化 + genre/必填过滤 ──
    valid = [c for c in (_normalize(p) for p in parsed) if c]
    if not valid:
        return None

    # ── 锁定再生成：用 locked 值确定性覆盖被锁字段（不信任 LLM 是否真的保持）──
    if locked and locked_fields:
        for c in valid:
            for lf in locked_fields:
                if lf not in c:
                    continue
                if lf == "power_ranks":
                    c[lf] = _norm_ranks(locked.get(lf))
                else:
                    lv = locked.get(lf)
                    c[lf] = str(lv).strip()[:_MAX_LEN] if lv else None
        # 覆盖后可能破坏必填字段（如锁了空值），重新过滤
        valid = [c for c in valid if all(c.get(f) for f in _REQUIRED_FIELDS)]
        if not valid:
            return None

    # ── 多样性硬判据：power_system 名与 main_conflict 两两不同（被锁字段跳过）──
    if len(valid) >= 2:
        for f in ("power_system", "main_conflict"):
            if f in locked_fields:
                continue
            vals = [c.get(f) or "" for c in valid]
            if len(set(vals)) < len(vals):
                return None

    valid = valid[:n]
    if require_n and len(valid) < n:
        return None
    return valid


# ── 确定性兜底 ─────────────────────────────────────────────────────────────────

def _fallback_candidate(ws: dict) -> dict:
    """degraded / 解析全失败时返回 1 条 genre 合法、必填字段非脏的占位候选。"""
    genre = ws.get("genre")
    if genre not in GENRE_VALUES:
        genre = "default"
    locked = ws.get("locked") or {}
    return {
        "genre": genre,
        "premise": locked.get("premise") or "（示例）生成暂不可用，可在手填模式自行填写。",
        "world_setting": locked.get("world_setting") or "",
        "power_system": locked.get("power_system") or "（示例）力量体系",
        "power_ranks": locked.get("power_ranks") or [],
        "protagonist": locked.get("protagonist") or "（示例）主角",
        "main_conflict": locked.get("main_conflict") or "（示例）核心冲突",
        "opening_hook": locked.get("opening_hook") or "",
        "target_audience": locked.get("target_audience") or "",
        "tone": locked.get("tone") or "",
    }
