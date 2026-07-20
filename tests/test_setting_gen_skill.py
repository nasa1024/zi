"""SettingGenSkill 单元测试（FakeProvider，无网络，无 DB）。

覆盖：JSON 解析与截断抢救、多样性硬判据、degraded 确定性兜底、
run() 全程不读 ctx.conn、单字段 reroll + avoid、锁定再生成保持锁定字段。
"""
from __future__ import annotations

import pytest

from novelforge.app.models import GENRE_VALUES
from novelforge.control_plane.budget import BudgetLedger
from novelforge.control_plane.llm.fake_provider import FakeProvider
from novelforge.control_plane.llm.gateway import LLMGateway
from novelforge.control_plane.skill_base import SkillContext
from novelforge.skills.setting_gen_skill import (
    SettingGenSkill, _parse_candidates, _fallback_candidate,
)


VALID_SETTINGS = """\
```settings
[
  {"genre":"xianxia","premise":"剑修逆袭","world_setting":"九州大陆","power_system":"剑道","power_ranks":["练气","筑基"],"protagonist":"林尘","main_conflict":"家族压迫与夺嫡","opening_hook":"祭祖剑鸣","target_audience":"男频","tone":"热血"},
  {"genre":"xianxia","premise":"丹师崛起","world_setting":"丹霞界","power_system":"丹道","power_ranks":["一品","二品"],"protagonist":"苏丹","main_conflict":"宗门夺宝阴谋","opening_hook":"古丹炉异象","target_audience":"男频","tone":"轻松"},
  {"genre":"xianxia","premise":"阵法宗师","world_setting":"万阵之域","power_system":"阵道","power_ranks":["初阶","中阶"],"protagonist":"周阵","main_conflict":"上古遗族复苏","opening_hook":"残破阵盘","target_audience":"男频","tone":"硬核"}
]
```
"""


class _ConnSpy:
    """任何属性访问都抛错——用于断言 SettingGenSkill.run 全程不碰 ctx.conn。"""

    def __getattr__(self, name):  # noqa: D401
        raise AssertionError(f"SettingGenSkill 不应访问 ctx.conn（触及了 .{name}）")


def _gateway(factory=None, responses=None) -> LLMGateway:
    prov = FakeProvider(factory=factory, responses=responses)
    return LLMGateway(prov, BudgetLedger(max_tokens=50_000, max_usd=1.0))


def _ctx(gw: LLMGateway, ws: dict) -> SkillContext:
    return SkillContext(
        project_id="setup", target_chapter=0, mode="human_gate",
        as_of_chapter=0, budget=gw.ledger, llm=gw, conn=_ConnSpy(), workspace=ws,
    )


# ── _parse_candidates ──────────────────────────────────────────────────────────

class TestParseCandidates:
    def test_happy_array(self):
        out = _parse_candidates(VALID_SETTINGS, n=3)
        assert isinstance(out, list) and len(out) == 3
        assert {c["power_system"] for c in out} == {"剑道", "丹道", "阵道"}

    def test_truncated_recovery(self):
        """JSON 被截断（缺收尾符号）也能抢救出已完成的候选。"""
        truncated = """\
```settings
[
  {"genre":"xuanhuan","premise":"少年崛起","power_system":"斗气","protagonist":"萧炎","main_conflict":"退婚之耻"},
  {"genre":"xuanhuan","premise":"魂师传奇","power_system":"武魂","protagonist":"唐三","main_conflict":"宗门倾覆"
"""
        out = _parse_candidates(truncated, n=3)
        assert isinstance(out, list) and len(out) >= 1

    def test_require_n_rejects_insufficient_candidates(self):
        """端点请求 N 个完整候选时，少于 N 不能伪装成正常成功。"""
        one = """```settings
[
  {"genre":"xuanhuan","premise":"少年崛起","power_system":"斗气","protagonist":"萧炎","main_conflict":"退婚之耻"}
]
```"""
        assert _parse_candidates(one, n=3, require_n=True) is None

    def test_illegal_genre_filtered(self):
        bad = """```settings
[{"genre":"NOT_A_GENRE","premise":"x","power_system":"a","protagonist":"b","main_conflict":"c"}]
```"""
        out = _parse_candidates(bad, n=1)
        # 唯一候选 genre 非法 → 无可用候选
        assert out is None or out == []

    def test_diversity_rejected(self):
        """多候选 power_system 名重复 → 视为同质，返回 None 触发升档/兜底。"""
        dup = """```settings
[
 {"genre":"xianxia","premise":"a","power_system":"剑道","protagonist":"甲","main_conflict":"冲突甲"},
 {"genre":"xianxia","premise":"b","power_system":"剑道","protagonist":"乙","main_conflict":"冲突乙"}
]
```"""
        assert _parse_candidates(dup, n=2) is None

    def test_min_fields_required(self):
        """缺 premise/protagonist/main_conflict 的候选不合格。"""
        missing = """```settings
[{"genre":"xianxia","premise":"","power_system":"剑道","protagonist":"","main_conflict":""}]
```"""
        out = _parse_candidates(missing, n=1)
        assert out is None or out == []

    def test_field_reroll_merges_locked(self):
        """field reroll：只取目标字段，与 locked 合并成完整一条。"""
        locked = {"genre": "xianxia", "premise": "原设定", "protagonist": "林尘",
                  "main_conflict": "家族压迫", "power_system": "旧体系"}
        text = """```settings
[{"power_system":"全新剑道体系"}]
```"""
        out = _parse_candidates(text, n=1, field="power_system", locked=locked, avoid=["旧体系"])
        assert isinstance(out, list) and len(out) == 1
        assert out[0]["power_system"] == "全新剑道体系"
        assert out[0]["protagonist"] == "林尘"          # locked 字段保留

    def test_field_reroll_avoid_rejected(self):
        """field reroll 产出值落在 avoid 中 → 不合格（要求换一个）。"""
        locked = {"genre": "xianxia", "premise": "p", "protagonist": "林尘",
                  "main_conflict": "c", "power_system": "旧体系"}
        text = """```settings
[{"power_system":"旧体系"}]
```"""
        out = _parse_candidates(text, n=1, field="power_system", locked=locked, avoid=["旧体系"])
        assert out is None or out == []

    def test_english_field_reroll_rejects_cross_language_value(self):
        locked = {
            "genre": "fantasy",
            "premise": "A courier climbs a living tower.",
            "protagonist": "Mara is a careful courier.",
            "main_conflict": "Each victory costs her a memory.",
            "power_system": "Memory Oaths",
        }
        text = '```settings\n[{"power_system":"全新剑道体系"}]\n```'

        assert _parse_candidates(
            text,
            n=1,
            field="power_system",
            locked=locked,
            content_language="en-US",
        ) is None

    def test_english_candidate_rejects_any_cross_language_visible_field(self):
        text = """```settings
[{"genre":"fantasy","premise":"A courier climbs a living tower.",
"world_setting":"九州大陆","power_system":"Memory Oaths",
"protagonist":"Mara is a careful courier.",
"main_conflict":"Each victory costs her a memory."}]
```"""

        assert _parse_candidates(
            text, n=1, content_language="en-US",
        ) is None


# ── _fallback_candidate ──────────────────────────────────────────────────────

class TestFallback:
    def test_legal_and_nonempty(self):
        c = _fallback_candidate({"genre": "kehuan"})
        assert c["genre"] in GENRE_VALUES
        assert c["premise"] and c["protagonist"] and c["main_conflict"]

    def test_illegal_genre_coerced(self):
        c = _fallback_candidate({"genre": "NOPE"})
        assert c["genre"] in GENRE_VALUES


# ── SettingGenSkill.run ──────────────────────────────────────────────────────

class TestRun:
    def test_run_with_valid_provider(self):
        gw = _gateway(factory=lambda messages, model: VALID_SETTINGS)
        ws = {"genre": "xianxia", "n": 3}
        skill = SettingGenSkill()
        res = skill.run(_ctx(gw, ws))
        assert res.ok
        assert len(ws["candidates"]) == 3
        assert all(c["genre"] in GENRE_VALUES for c in ws["candidates"])
        assert ws.get("tier_used")

    def test_run_does_not_touch_conn(self):
        """_ConnSpy 任意属性访问都抛错；run 正常完成即证明未碰 conn。"""
        gw = _gateway(factory=lambda messages, model: VALID_SETTINGS)
        ws = {"genre": "xianxia", "n": 3}
        SettingGenSkill().run(_ctx(gw, ws))   # 不抛 AssertionError 即通过
        assert ws["candidates"]

    def test_degraded_fallback(self):
        """无 factory 的 FakeProvider 只回显 → 解析失败 → 确定性兜底 ≥1 条。"""
        gw = _gateway()  # 默认回显，产不出合法 JSON
        ws = {"genre": "wuxia", "n": 3}
        res = SettingGenSkill().run(_ctx(gw, ws))
        assert ws["degraded"] is True
        assert ws["fallback_reason"] == "parse_failed_or_insufficient_candidates"
        assert len(ws["candidates"]) >= 1
        c = ws["candidates"][0]
        assert c["genre"] in GENRE_VALUES
        assert c["premise"] and c["protagonist"] and c["main_conflict"]
        assert res.ok  # 兜底也算产出

    def test_underproduced_candidates_degrade(self):
        """真实 provider 只返回 1 条但请求 3 条时，走兜底并标 degraded。"""
        one = """```settings
[
  {"genre":"xuanhuan","premise":"少年崛起","power_system":"斗气","protagonist":"萧炎","main_conflict":"退婚之耻"}
]
```"""
        gw = _gateway(factory=lambda messages, model: one)
        ws = {"genre": "xuanhuan", "n": 3}
        SettingGenSkill().run(_ctx(gw, ws))
        assert ws["degraded"] is True
        assert len(ws["candidates"]) == 1

    def test_field_reroll_forces_single(self):
        """给 field 时强制 n=1。"""
        gw = _gateway(factory=lambda messages, model: '```settings\n[{"power_system":"新体系"}]\n```')
        ws = {"genre": "xianxia", "n": 3, "field": "power_system",
              "locked": {"genre": "xianxia", "premise": "p", "protagonist": "甲",
                         "main_conflict": "c", "power_system": "旧"},
              "avoid": ["旧"]}
        SettingGenSkill().run(_ctx(gw, ws))
        assert len(ws["candidates"]) == 1
        assert ws["candidates"][0]["power_system"] == "新体系"

    def test_locked_fields_preserved_in_multi(self):
        """锁定再生成：locked_fields 标注的字段在所有候选中保持锁定值（不信任 LLM 输出）。"""
        payload = """```settings
[
 {"genre":"xianxia","premise":"a","power_system":"剑道","protagonist":"路人甲","main_conflict":"冲突甲"},
 {"genre":"xianxia","premise":"b","power_system":"丹道","protagonist":"路人乙","main_conflict":"冲突乙"}
]
```"""
        gw = _gateway(factory=lambda messages, model: payload)
        ws = {"genre": "xianxia", "n": 2,
              "locked": {"genre": "xianxia", "protagonist": "林尘", "premise": "x",
                         "main_conflict": "y", "power_system": "z"},
              "locked_fields": ["protagonist"]}
        SettingGenSkill().run(_ctx(gw, ws))
        cands = ws["candidates"]
        assert len(cands) == 2
        assert all(c["protagonist"] == "林尘" for c in cands)

    def test_locked_field_skips_diversity(self):
        """锁定的字段不参与多样性校验（否则锁 power_system 后两候选必然同值被误拒）。"""
        payload = """```settings
[
 {"genre":"xianxia","premise":"a","power_system":"任意一","protagonist":"甲","main_conflict":"冲突甲"},
 {"genre":"xianxia","premise":"b","power_system":"任意二","protagonist":"乙","main_conflict":"冲突乙"}
]
```"""
        gw = _gateway(factory=lambda messages, model: payload)
        ws = {"genre": "xianxia", "n": 2,
              "locked": {"genre": "xianxia", "power_system": "祖传剑道", "protagonist": "甲基",
                         "premise": "p", "main_conflict": "mc"},
              "locked_fields": ["power_system"]}
        SettingGenSkill().run(_ctx(gw, ws))
        cands = ws["candidates"]
        assert len(cands) == 2                       # 没被多样性误拒
        assert all(c["power_system"] == "祖传剑道" for c in cands)
