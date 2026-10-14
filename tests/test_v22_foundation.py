"""v22 foundation: contract fields, extension registry and prompt plumbing."""
from __future__ import annotations

import json

import pytest

from novelforge.craft import extensions
from novelforge.planning.context import (
    PlanContext,
    _contract_turn_lines,
    _promise_action_lines,
)
from novelforge.planning.contract_fields import (
    normalize_polarity,
    normalize_promise_actions,
    normalize_turning_point,
    normalize_witnesses,
)
from novelforge.planning.models import ChapterActuals, ChapterPlanDraft
from novelforge.skills.chapter_draft_skill import _fmt_beats


class TestContractFields:
    @pytest.mark.parametrize("raw,expected", [
        ("+", "+"), ("Positive", "+"), ("扬", "+"),
        ("-", "-"), ("负", "-"), ("虐", "-"),
        ("±", "±"), ("+/-", "±"), ("得失参半", "±"),
        ("sideways", None), ("", None), (None, None),
    ])
    def test_polarity(self, raw, expected):
        assert normalize_polarity(raw) == expected

    def test_turning_point(self):
        assert normalize_turning_point("揭示") == "revelation"
        assert normalize_turning_point("ACTION") == "action"
        assert normalize_turning_point("vibes") is None

    def test_promise_actions_drop_unusable_items(self):
        out = normalize_promise_actions([
            {"label": "血仇", "action": "提醒"},
            {"promise_id": "fs1", "action": "payoff", "note": "当众"},
            {"label": "无动作"},
            {"action": "advance"},
            "garbage",
        ])
        assert out == [
            {"promise_id": None, "label": "血仇", "action": "remind", "note": None},
            {"promise_id": "fs1", "label": None, "action": "payoff", "note": "当众"},
        ]
        assert normalize_promise_actions("not a list") == []

    def test_witnesses(self):
        assert normalize_witnesses("张三，李四、张三") == ["张三", "李四"]
        assert normalize_witnesses(["Ann", "", "Bo"]) == ["Ann", "Bo"]
        assert normalize_witnesses(42) == []

    def test_models_accept_lenient_values(self):
        plan = ChapterPlanDraft(
            chapter=3, goal="g", polarity="负", turning_point="揭秘",
            promise_actions=[{"label": "血仇", "action": "推进"}, {"action": "bad"}],
            beats=[{"seq": 1, "beat_type": "aftermath", "summary": "余韵",
                    "witnesses": "王长老, 赵师兄"}],
        )
        assert (plan.polarity, plan.turning_point) == ("-", "revelation")
        assert [a.action for a in plan.promise_actions] == ["advance"]
        assert plan.beats[0].witnesses == ["王长老", "赵师兄"]
        legacy = ChapterPlanDraft(chapter=1, goal="g")
        assert legacy.polarity is None and legacy.promise_actions == []
        actuals = ChapterActuals(chapter=1, polarity="+/-")
        assert actuals.polarity == "±" and actuals.reactions == []


class TestRendering:
    def test_contract_lines(self):
        contract = {
            "polarity": "+", "turning_point": "action",
            "promise_actions": [{"promise_id": "fs1", "label": "血仇", "action": "remind"}],
        }
        assert _contract_turn_lines(contract, "zh")[0].startswith("- 本章转向：+")
        assert _contract_turn_lines(contract, "en")[1].startswith("- Turning point: action")
        assert _promise_action_lines(contract, "zh") == ["提醒：血仇（fs1）"]

    def test_contract_render_includes_v22_fields(self):
        ctx = PlanContext(
            target_chapter=3,
            current_contract={
                "chapter": 3, "goal": "夺回玉佩", "polarity": "-",
                "promise_actions": [{"label": "血仇", "action": "remind"}],
            },
        )
        zh = ctx.render_contract_for_draft("zh-CN")
        assert "本章转向" in zh and "提醒：血仇" in zh
        en = ctx.render_contract_for_draft("en-US")
        assert "Turn direction" in en and "remind: 血仇" in en

    def test_fmt_beats_renders_witnesses(self):
        text = _fmt_beats([
            {"beat_type": "payoff_beat", "summary": "当众打脸", "value_axis": "轻视→敬畏",
             "witnesses": ["王长老"]},
            {"beat_type": "hook", "summary": "黑影", "value_axis": ""},
        ])
        assert "(witnesses: 王长老)" in text
        assert "黑影" in text and text.count("witnesses") == 1

    def test_fmt_beats_splits_string_witnesses_by_name(self):
        # LLM 常把 witnesses 回成分隔字符串：必须按名字拆，而不是逐字符展开
        text = _fmt_beats([
            {"beat_type": "payoff_beat", "summary": "打脸", "value_axis": "x",
             "witnesses": "王长老、赵师兄"},
        ])
        assert "(witnesses: 王长老, 赵师兄)" in text
        assert "王, 长" not in text

    def test_planner_parse_canonicalizes_string_witnesses(self):
        from novelforge.skills.planner_skill import _parse_beats_strict

        beats = _parse_beats_strict(json.dumps([
            {"beat_type": "payoff_beat", "summary": "当众打脸",
             "witnesses": "王长老、赵师兄"},
            {"beat_type": "hook", "summary": "黑影"},
        ], ensure_ascii=False))
        assert beats is not None
        assert beats[0]["witnesses"] == ["王长老", "赵师兄"]
        assert "witnesses" not in beats[1]


class TestExtensionRegistry:
    def test_failing_extension_is_isolated(self, monkeypatch):
        import novelforge.craft.polarity as polarity_mod

        def boom(_ctx):
            raise RuntimeError("boom")

        monkeypatch.setattr(polarity_mod, "craft_checks", boom, raising=False)
        assert extensions.run_craft_checks(object()) == []

    def test_import_time_error_is_isolated_and_logged_once(self, monkeypatch):
        # review：_load 只捕获 ImportError，模块级 NameError 会让 run_craft_checks /
        # settle_steps 整体抛出（结算阶段会把已提交的章节标成 crashed）。
        real_import = extensions.importlib.import_module
        calls: list[str] = []

        def fake_import(name, *args, **kwargs):
            if name.endswith(".polarity"):
                calls.append(name)
                raise NameError("broken module")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(extensions, "_BROKEN", set())
        monkeypatch.setattr(extensions.importlib, "import_module", fake_import)
        assert isinstance(extensions.run_craft_checks(object()), list)
        names = [name for name, _ in extensions.settle_steps()]
        assert "polarity" not in names and "promise_ledger" in names
        extensions.enrich_actuals(None, "", None)
        assert len(calls) == 1          # 坏模块只尝试导入一次

    def test_issue_normalisation(self, monkeypatch):
        import novelforge.craft.promise_ledger as promise_mod

        monkeypatch.setattr(promise_mod, "craft_checks", lambda _ctx: [
            {"check": "promise_dormant", "severity": "fatal", "detail": "长线休眠",
             "repair_scope": "galaxy"},
            {"check": "empty"},
            "junk",
        ], raising=False)
        issues = extensions.run_craft_checks(object())
        assert issues == [{
            "check": "promise_dormant", "severity": "warn", "detail": "长线休眠",
            "span": "", "fix": "", "repair_scope": "local",
            "category": "craft.promise_dormant",
        }]

    def test_briefs_join_non_empty_blocks(self, monkeypatch):
        import novelforge.craft.payoff_reactions as payoff_mod
        import novelforge.craft.promise_ledger as promise_mod

        monkeypatch.setattr(promise_mod, "draft_brief",
                            lambda *a, **k: "## 承诺提醒\n- 血仇", raising=False)
        monkeypatch.setattr(payoff_mod, "draft_brief", lambda *a, **k: "  ", raising=False)
        out = extensions.build_draft_briefs(None, 3, {}, language="zh-CN", cfg=None)
        assert out == "## 承诺提醒\n- 血仇"

    def test_craft_check_skill_surfaces_extension_issues(self, monkeypatch):
        import sqlite3

        import novelforge.craft.deus_ex_machina as dem_mod
        from novelforge.control_plane.skill_base import SkillContext
        from novelforge.db.connection import init_db_from_conn
        from novelforge.skills.craft_check_skill import CraftCheckSkill

        monkeypatch.setattr(dem_mod, "craft_checks", lambda _ctx: [
            {"check": "deus_ex_machina", "severity": "warn", "detail": "天降外挂"},
        ], raising=False)
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db_from_conn(conn)
        ctx = SkillContext(
            project_id="p", target_chapter=2, mode="human_gate", as_of_chapter=1,
            budget=None, llm=None, conn=conn,
            workspace={"draft_text": "正文", "beats": [
                {"beat_type": "setup", "summary": "开场", "value_axis": "平静→紧张"},
                {"beat_type": "aftermath", "summary": "余韵", "value_axis": "紧→松"},
                {"beat_type": "hook", "summary": "悬念", "value_axis": "悬念↑"},
            ]},
        )
        CraftCheckSkill().run(ctx)
        checks = [(i["check"], i["severity"]) for i in ctx.workspace["craft_issues"]]
        assert ("deus_ex_machina", "warn") in checks
        # aftermath is a legal beat type in v22
        assert not any(c == "beat_contract" for c, _ in checks)
