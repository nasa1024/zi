"""v22/F3 极性序列与抑扬上限：实际转向回填、逐章结算与重算、告警、提示块、节拍游标。

全部离线：FakeProvider / 内存或临时 SQLite，无网络。
"""
from __future__ import annotations

import json
import re
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from novelforge.config import NovelForgeConfig
from novelforge.craft import polarity
from novelforge.craft.pacing import PacingController, PacingState
from novelforge.planning.actuals import extract_chapter_actuals
from novelforge.planning.context import PlanContext
from novelforge.planning.models import BeatEvidence, ChapterActuals

_CJK = re.compile(r"[㐀-鿿]")
# 与 FakeProvider 路由冲突的提示词标记（COMMON.md 硬约束 2）
_ROUTING_MARKERS = (
    "规划任务", "## Planning Task", "本章任务", "## Chapter Task", "草稿：", "### 候选",
    "本章目标：", "Chapter goal: ", "一致性问题", "修订补丁任务", "润色补丁任务", "工艺问题",
    "未解伏笔", "章正文：", "各章摘要", "卷《", "章正文如下：", "场景原文",
)


# ── fixtures / helpers ───────────────────────────────────────────────────────

@pytest.fixture
def conn():
    from novelforge.db.connection import init_db_from_conn
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db_from_conn(c)
    yield c
    c.close()


def _seed(conn, rows):
    """rows: [(chapter, polarity, tension_level)] → pacing_state."""
    for chapter, pol, level in rows:
        conn.execute(
            "INSERT INTO pacing_state(id, chapter, tension_level, polarity) VALUES(?,?,?,?)",
            (f"ps{chapter}", chapter, level, pol),
        )
    conn.commit()


def _ctx(conn, chapter, *, beats=None, contract=None, language="zh-CN", settings=None):
    workspace = {
        "beats": beats or [],
        "draft_text": "正文",
        "writing_profile": {"content_language": language},
    }
    if contract is not None:
        workspace["plan_context"] = PlanContext(target_chapter=chapter,
                                                current_contract=contract)
    if settings is not None:
        workspace[polarity.SETTINGS_KEY] = settings
    return SimpleNamespace(conn=conn, target_chapter=chapter, workspace=workspace,
                           extra={}, llm=None)


def _beat(beat_type, summary="", axis=None, seq=None):
    beat = {"beat_type": beat_type, "summary": summary or beat_type, "value_axis": axis}
    if seq is not None:
        beat["seq"] = seq
    return beat


_NEUTRAL_BEATS = [_beat("setup", axis="power"), _beat("turn"), _beat("hook", axis="悬念↑")]


# ── 1. 价值轴推断（词典 + 方向）─────────────────────────────────────────────

class TestAxisPolarity:
    @pytest.mark.parametrize("axis,expected", [
        ("绝境→翻盘", "+"),
        ("平静→波澜", "-"),
        ("胜利→重伤", "-"),
        ("危机→惨胜", "±"),                 # 终态得失参半
        ("从绝望到希望", "+"),
        ("希望↑", "+"),
        ("权力↓", "-"),
        ("危机↑", "-"),
        ("被困→逃出生天", "+"),             # 负 → 中性也是好转
        ("失去了希望→希望", "+"),           # 紧邻否定翻转
        ("危机→化解危机", "+"),             # 化解翻转负面词
        ("safe -> trapped", "-"),
        ("despair → hope", "+"),
        ("trust ↓", "-"),
        ("status down", "-"),
        ("from hope to despair", "-"),
        ("victory -> pyrrhic victory", "±"),
        # 没有方向的轴名 / 中性状态 → 不猜
        ("power", None), ("生死", None), ("悬念↑", None), ("守→攻", None),
        ("tension↑", None), ("", None), (None, None),
    ])
    def test_axis(self, axis, expected):
        assert polarity.axis_polarity(axis) == expected

    @pytest.mark.parametrize("axis,expected", [
        # "<轴名>：A→B"：轴名（价值物）不能被算成起态的正面词（review r1：曾得 "-"）
        ("实力：弱→强", "+"), ("地位：低→高", "+"), ("信任：破裂→修复", "+"),
        ("生死：安全→濒死", "-"), ("实力：强→弱", "-"), ("实力：从弱到强", "+"),
        ("希望：破灭→重燃", "+"), ("林凡：落魄→崛起", "+"), ("主角处境：安全→危机", "-"),
        ("实力：弱→强（突破）", "+"),
        # 没有冒号的同一写法
        ("实力 弱→强", "+"), ("实力弱→强", "+"), ("实力弱→实力强", "+"), ("弱→实力强", "+"),
        ("强→失败", "-"),                   # 不带量级词的情感词仍是状态，不当轴名
        # 量级词的好坏由轴名决定
        ("危险：低→高", "-"), ("压力：小→大", "-"), ("恐惧：强→弱", "+"),
        ("伤势：轻→重", "-"), ("危机→更大的危机", "-"),
        # 对手的价值物取反
        ("敌人实力：弱→强", "-"), ("对手实力↓", "+"),
        # English
        ("status: low -> high", "+"), ("Status: from low to high", "+"),
        ("power: lost -> regained", "+"), ("trust: broken -> restored", "+"),
        ("power: weak -> strong", "+"), ("status low -> high", "+"),
        ("danger: low -> high", "-"), ("rival status: low -> high", "-"),
        ("enemy power ↑", "-"), ("Mara's standing: outcast -> accepted", "+"),
        # 无轴名的纯量级变化 → 不猜
        ("弱→强", None), ("low -> high", None),
    ])
    def test_axis_label_and_magnitude(self, axis, expected):
        assert polarity.axis_polarity(axis) == expected

    def test_far_negator_does_not_flip(self):
        assert polarity.state_value("无敌的希望") == "+"

    @pytest.mark.parametrize("signs,expected", [
        (["+"], "+"), (["-"], "-"), (["±"], "±"),
        (["-", "+"], "+"),            # 抑后扬：高潮权重 ×2
        (["+", "-"], "-"),            # 乐极生悲
        (["-", "-", "+"], "±"),       # 久压小胜：持平 → 得失参半
        (["+", "±"], "±"),            # 高潮 beat 本身得失参半
        ([None, "+"], "+"), ([], None), ([None], None),
    ])
    def test_aggregate(self, signs, expected):
        assert polarity.aggregate_turns(signs) == expected

    def test_hook_beats_do_not_carry_the_turn(self):
        beats = [_beat("setup", axis="危机→翻盘"), _beat("hook", axis="平安→危机")]
        assert polarity.infer_polarity(beats) == "+"

    def test_turning_point_inference(self):
        assert polarity.infer_turning_point(
            [_beat("turn", "林动识破长老的阴谋")]) == "revelation"
        assert polarity.infer_turning_point(
            [_beat("payoff_beat", "He defeats the champion")]) == "action"
        assert polarity.infer_turning_point([_beat("setup", "林动发现真相")]) is None


# ── 2. enrich_actuals ───────────────────────────────────────────────────────

def _plan(polarity_value=None, turning=None, beats=None):
    contract = {"chapter": 5, "goal": "林动查清真相", "beats": beats or []}
    if polarity_value is not None:
        contract["polarity"] = polarity_value
    if turning is not None:
        contract["turning_point"] = turning
    return {"current_contract": contract}


_PLANNED_BEATS = [
    _beat("setup", "林动潜入藏经阁", "平静→危机", 1),
    _beat("turn", "林动识破长老的阴谋", "绝境→翻盘", 2),
    _beat("hook", "神秘人现身", "悬念↑", 3),
]


class TestEnrichActuals:
    def test_executed_plan_uses_contract(self):
        draft = "林动潜入藏经阁。他屏住呼吸。林动识破长老的阴谋。夜色沉沉。"
        actuals = extract_chapter_actuals(
            5, draft, _plan("-", "revelation", _PLANNED_BEATS)).actuals
        assert sum(b.completed for b in actuals.completed_beats) == 2
        assert actuals.polarity == "-"            # 契约优先于词典推断（+）
        assert actuals.turning_point == "revelation"

    def test_executed_plan_without_contract_turning_point_infers_it(self):
        draft = "林动潜入藏经阁。林动识破长老的阴谋。"
        actuals = extract_chapter_actuals(5, draft, _plan("+", None, _PLANNED_BEATS)).actuals
        assert actuals.polarity == "+"
        assert actuals.turning_point == "revelation"

    def test_unexecuted_plan_falls_back_to_completed_value_axes(self):
        # 只完成 1/3：契约的 "-" 不可信，按已完成 beat 的价值轴推断
        draft = "风雪夜。林动识破长老的阴谋。众人沉默。"
        actuals = extract_chapter_actuals(5, draft, _plan("-", None, _PLANNED_BEATS)).actuals
        assert sum(b.completed for b in actuals.completed_beats) == 1
        assert actuals.polarity == "+"
        assert actuals.turning_point == "revelation"

    def test_value_axis_inference_without_contract_polarity(self):
        draft = "林动潜入藏经阁。林动识破长老的阴谋。神秘人现身。"
        actuals = extract_chapter_actuals(5, draft, _plan(None, None, _PLANNED_BEATS)).actuals
        assert actuals.polarity == "+"            # [-, +]：抑后扬

    def test_english_value_axes(self):
        beats = [
            _beat("setup", "Mara reaches the archive", "calm -> danger", 1),
            _beat("turn", "Mara learns the duke lied", "trust ↓", 2),
            _beat("hook", "Footsteps behind her", "suspense ↑", 3),
        ]
        draft = "Mara reaches the archive. The ledger is gone. Mara learns the duke lied."
        actuals = extract_chapter_actuals(5, draft, _plan(None, None, beats)).actuals
        assert actuals.polarity == "-"
        assert actuals.turning_point == "revelation"

    def test_unknown_stays_none(self):
        beats = [_beat("setup", "开场", "power", 1), _beat("hook", "悬念", "悬念↑", 2)]
        actuals = extract_chapter_actuals(5, "开场。悬念。", _plan(None, None, beats)).actuals
        assert actuals.polarity is None and actuals.turning_point is None
        # 没有契约 beats：无从判断
        actuals = extract_chapter_actuals(5, "正文。", None).actuals
        assert actuals.polarity is None

    def test_never_raises_and_respects_existing_value(self):
        polarity.enrich_actuals(object(), "x", {"current_contract": {"beats": "garbage"}})
        polarity.enrich_actuals(None, "x", None)
        actuals = ChapterActuals(chapter=2, polarity="±",
                                 completed_beats=[BeatEvidence(beat_seq=1, completed=True)])
        polarity.enrich_actuals(actuals, "x", _plan("-", None, _PLANNED_BEATS))
        assert actuals.polarity == "±"


# ── 3. settle：逐章 upsert + 重算游标 ────────────────────────────────────────

def _settle(conn, chapter, pol=None, *, beats=None, draft="正文", cfg=None, actuals=True):
    workspace = {"beats": beats if beats is not None else [_beat("setup"), _beat("hook")],
                 "draft_text": draft}
    if actuals:
        workspace["chapter_actuals"] = ChapterActuals(chapter=chapter, polarity=pol)
    report = polarity.settle(conn, chapter, workspace, gateway=None, cfg=cfg)
    conn.commit()
    return report


def _cursor(conn):
    return dict(conn.execute(
        "SELECT low_streak, polarity_streak, last_polarity FROM pacing_cursor WHERE id=1"
    ).fetchone())


class TestSettle:
    def test_streaks_are_recomputed_and_idempotent(self, conn):
        for ch in (1, 2, 3):
            report = _settle(conn, ch, "-")
        assert report["low_streak"] == 3 and report["polarity_streak"] == 3
        assert _cursor(conn) == {"low_streak": 3, "polarity_streak": 3, "last_polarity": "-"}

        _settle(conn, 3, "-")                      # 重写同一章：不累加
        assert _cursor(conn)["low_streak"] == 3
        assert conn.execute("SELECT COUNT(*) FROM pacing_state").fetchone()[0] == 3

        _settle(conn, 2, "+")                      # 换稿改变历史：按历史重算
        assert _cursor(conn) == {"low_streak": 1, "polarity_streak": 1, "last_polarity": "-"}

        _settle(conn, 3, "±")                      # ± 打断低谷
        assert _cursor(conn) == {"low_streak": 0, "polarity_streak": 1, "last_polarity": "±"}

    def test_gap_or_unknown_polarity_breaks_the_streak(self, conn):
        for ch in (1, 2, 4):
            _settle(conn, ch, "+")
        assert _cursor(conn)["polarity_streak"] == 1   # 第 3 章缺失
        _settle(conn, 3, "+")
        assert _cursor(conn)["polarity_streak"] == 4
        _settle(conn, 5, None)
        assert _cursor(conn) == {"low_streak": 0, "polarity_streak": 0, "last_polarity": None}

    def test_row_content(self, conn):
        beats = [_beat("setup"), _beat("tension_point"), _beat("payoff_beat"), _beat("hook")]
        conn.execute("UPDATE pacing_cursor SET buildup=7 WHERE id=1")
        actuals = ChapterActuals(chapter=1, polarity="+", turning_point="action")
        report = polarity.settle(conn, 1, {"beats": beats, "draft_text": "正文",
                                           "chapter_actuals": actuals}, cfg=None)
        row = conn.execute("SELECT * FROM pacing_state WHERE chapter=1").fetchone()
        assert (row["polarity"], row["turning_point"]) == ("+", "action")
        assert (row["tension_level"], row["pace"]) == (5, "climax")
        notes = json.loads(row["notes"])
        assert notes["source"] == "actuals" and notes["climax"] == 5
        assert notes["beat_types"] == ["setup", "tension_point", "payoff_beat", "hook"]
        assert report["source"] == "actuals" and report["last_polarity"] == "+"

    @pytest.mark.parametrize("types,level,pace", [
        (["setup", "aftermath", "hook"], 1, "slow"),
        (["setup", "turn", "hook"], 2, "steady"),
        (["tension_point", "hook"], 3, "fast"),
        (["payoff_beat", "hook"], 4, "climax"),
        (["payoff_beat", "payoff_beat"], 5, "climax"),
    ])
    def test_tension_scale(self, conn, types, level, pace):
        report = _settle(conn, 1, "+", beats=[_beat(t) for t in types])
        assert (report["tension_level"], report["pace"]) == (level, pace)

    def test_fallback_infers_from_planner_beats_in_final_text(self, conn):
        beats = [_beat("setup", "林动被困在血色秘境", "平静→危机"),
                 _beat("turn", "林动识破长老的阴谋", "绝境→翻盘"),
                 _beat("hook", "神秘人现身", "悬念↑")]
        draft = "林动被困在血色秘境。四下无人。林动识破长老的阴谋。神秘人现身。"
        report = _settle(conn, 1, beats=beats, draft=draft, actuals=False)
        assert (report["polarity"], report["turning_point"], report["source"]) == (
            "+", "revelation", "beats")
        # 草稿里找不到 beat 证据 → 不猜
        report = _settle(conn, 2, beats=beats, draft="平稳正文。" * 20, actuals=False)
        assert report["polarity"] is None and report["source"] is None

    def test_planner_beats_used_when_unlocked_contract_beats_were_not_written(self, conn):
        # 契约 beats 未完成（actuals 无转向），planner 另写的 beats 在终稿中有证据
        actuals = ChapterActuals(chapter=1, completed_beats=[
            BeatEvidence(beat_seq=1, completed=False)])
        planner_beats = [_beat("turn", "林动识破长老的阴谋", "绝境→翻盘"), _beat("hook")]
        report = polarity.settle(conn, 1, {"beats": planner_beats, "chapter_actuals": actuals,
                                           "draft_text": "林动识破长老的阴谋。"}, cfg=None)
        assert (report["polarity"], report["source"]) == ("+", "beats")
        report = polarity.settle(conn, 2, {"beats": planner_beats, "chapter_actuals": actuals,
                                           "draft_text": "风平浪静。"}, cfg=None)
        assert report["polarity"] is None

    def test_pacing_update_preserves_polarity_columns(self, conn):
        for ch in (1, 2, 3):
            _settle(conn, ch, "-")
        PacingController().update(4, [_beat("tension_point")], 2000, conn)
        state = PacingController().get_state(conn)
        assert (state.low_streak, state.polarity_streak, state.last_polarity) == (3, 3, "-")
        assert state.needs_turn and state.recent_high_streak == 1

    def test_disabled_resets_counters_and_writes_nothing(self, conn):
        for ch in (1, 2, 3):
            _settle(conn, ch, "-")
        cfg = NovelForgeConfig()
        cfg.polarity.enabled = False
        assert _settle(conn, 4, "-", cfg=cfg) == {"enabled": False}
        assert _cursor(conn) == {"low_streak": 0, "polarity_streak": 0, "last_polarity": None}
        assert conn.execute(
            "SELECT COUNT(*) FROM pacing_state WHERE chapter=4").fetchone()[0] == 0

    @pytest.mark.parametrize("ddl,error", [
        (None, "no such table"),
        ("CREATE TABLE pacing_state (id TEXT PRIMARY KEY, chapter INTEGER NOT NULL UNIQUE,"
         " tension_level INTEGER NOT NULL, pace TEXT, value_shift_net TEXT, notes TEXT)",
         "has no column named polarity"),
    ])
    def test_pre_v22_database_is_reported_not_raised(self, ddl, error):
        legacy = sqlite3.connect(":memory:")
        legacy.row_factory = sqlite3.Row
        if ddl:
            legacy.execute(ddl)
        report = polarity.settle(legacy, 3, {"beats": [], "draft_text": "x",
                                             "chapter_actuals": ChapterActuals(
                                                 chapter=3, polarity="-")}, cfg=None)
        assert report["skipped"] == "schema" and error in report["error"]
        assert not legacy.in_transaction
        cfg = NovelForgeConfig()
        cfg.polarity.enabled = False
        assert polarity.settle(legacy, 3, {}, cfg=cfg) == {"enabled": False}


# ── 4. craft_checks ─────────────────────────────────────────────────────────

class TestCraftChecks:
    def test_silent_without_history(self, conn):
        ctx = _ctx(conn, 4, beats=_NEUTRAL_BEATS, contract={"polarity": "-"})
        assert polarity.craft_checks(ctx) == []

    def test_low_streak_with_negative_plan_warns_once(self, conn):
        _seed(conn, [(1, "-", 2), (2, "-", 2), (3, "-", 3)])
        issues = polarity.craft_checks(_ctx(conn, 4, beats=_NEUTRAL_BEATS,
                                            contract={"polarity": "-"}))
        assert len(issues) == 1           # 不与同向告警重复
        issue = issues[0]
        assert issue["check"] == "polarity_turn" and issue["severity"] == "warn"
        assert issue["category"] == "craft.polarity_turn"
        assert "第 1–3 章" in issue["detail"] and "转机" in issue["fix"]

    def test_low_streak_without_plan_polarity(self, conn):
        _seed(conn, [(1, "-", 2), (2, "-", 2), (3, "-", 3)])
        issues = polarity.craft_checks(_ctx(conn, 4, beats=_NEUTRAL_BEATS))
        assert [i["check"] for i in issues] == ["polarity_turn"]
        assert "没有安排明确的转机" in issues[0]["detail"]

    def test_planned_upturn_is_fine(self, conn):
        _seed(conn, [(1, "-", 2), (2, "-", 2), (3, "-", 3)])
        assert polarity.craft_checks(_ctx(conn, 4, contract={"polarity": "+"})) == []
        # 契约未定时，用本章 beats 的价值轴判断计划转向
        beats = [_beat("setup", axis="绝境→翻盘"), _beat("hook")]
        assert polarity.craft_checks(_ctx(conn, 4, beats=beats)) == []

    def test_positive_streak_needs_reverse_turn(self, conn):
        _seed(conn, [(1, "+", 4), (2, "+", 2), (3, "+", 2)])
        issues = polarity.craft_checks(_ctx(conn, 4, contract={"polarity": "+"}))
        assert [i["check"] for i in issues] == ["polarity_streak"]
        assert "4 章同向" in issues[0]["detail"] and "反向" in issues[0]["fix"]
        assert polarity.craft_checks(_ctx(conn, 4, contract={"polarity": "-"})) == []
        assert polarity.craft_checks(_ctx(conn, 3, contract={"polarity": "+"})) == []

    def test_gap_and_unknown_break_history(self, conn):
        _seed(conn, [(1, "-", 2), (2, "-", 2), (3, "-", 2)])
        assert polarity.craft_checks(_ctx(conn, 5, contract={"polarity": "-"})) == []
        conn.execute("INSERT INTO pacing_state(id, chapter, tension_level) VALUES('x', 4, 1)")
        assert polarity.craft_checks(_ctx(conn, 5, contract={"polarity": "-"})) == []

    def test_settings_from_workspace(self, conn):
        _seed(conn, [(1, "+", 2), (2, "+", 2)])
        settings = {"enabled": True, "max_same_polarity_streak": 2, "max_low_streak": 3}
        issues = polarity.craft_checks(_ctx(conn, 3, contract={"polarity": "+"},
                                            settings=settings))
        assert [i["check"] for i in issues] == ["polarity_streak"]
        off = {**settings, "enabled": False}
        assert polarity.craft_checks(_ctx(conn, 3, contract={"polarity": "+"},
                                          settings=off)) == []

    def test_english_project_gets_english_text(self, conn):
        _seed(conn, [(1, "-", 2), (2, "-", 2), (3, "-", 5)])
        beats = [_beat("tension_point", axis="calm -> danger"), _beat("hook")]
        issues = polarity.craft_checks(_ctx(conn, 4, beats=beats, language="en-US"))
        assert [i["check"] for i in issues] == ["polarity_turn", "aftermath"]
        for issue in issues:
            assert not _CJK.search(issue["detail"] + issue["fix"]), issue

    def test_english_positive_streak_text(self, conn):
        _seed(conn, [(1, "+", 2), (2, "+", 2), (3, "+", 2)])
        issues = polarity.craft_checks(_ctx(conn, 4, contract={"polarity": "+"},
                                            language="en-US"))
        assert [i["check"] for i in issues] == ["polarity_streak"]
        assert "4 in a row" in issues[0]["detail"]
        assert "Turn the other way" in issues[0]["fix"]
        assert not _CJK.search(issues[0]["detail"] + issues[0]["fix"])

    def test_axis_label_shape_does_not_fake_a_low_streak(self, conn):
        """"实力：弱→强" 一类的上扬章不能被算成低谷（review r1 的主要问题）。"""
        _seed(conn, [(1, "-", 2), (2, "-", 2), (3, "-", 2)])
        rising = [_beat("setup", axis="实力：弱→强"), _beat("turn", axis="地位：低→高"),
                  _beat("hook", axis="悬念↑")]
        assert polarity.craft_checks(_ctx(conn, 4, beats=rising)) == []
        en = [_beat("turn", axis="status: low -> high"), _beat("hook")]
        assert polarity.craft_checks(_ctx(conn, 4, beats=en, language="en-US")) == []

    def test_aftermath_after_big_payoff(self, conn):
        _seed(conn, [(3, "+", 5)])
        hot = [_beat("tension_point"), _beat("turn"), _beat("hook")]
        issues = polarity.craft_checks(_ctx(conn, 4, beats=hot))
        assert [i["check"] for i in issues] == ["aftermath"]
        assert issues[0]["repair_scope"] == "local" and "余韵" in issues[0]["fix"]
        calm = [_beat("setup"), _beat("tension_point"), _beat("hook")]
        cooled = [_beat("tension_point"), _beat("aftermath"), _beat("hook")]
        for beats in (calm, cooled, []):
            assert polarity.craft_checks(_ctx(conn, 4, beats=beats)) == []

    def test_lone_payoff_or_in_chapter_cooldown_needs_no_aftermath(self, conn):
        """小爽点（单个 payoff_beat，档位 4）不要求余韵；上一章已在章内平复也不要求。"""
        hot = [_beat("tension_point"), _beat("turn"), _beat("hook")]
        _seed(conn, [(3, "+", 4)])
        assert polarity.craft_checks(_ctx(conn, 4, beats=hot)) == []
        conn.execute("UPDATE pacing_state SET tension_level=5, notes=? WHERE chapter=3",
                     (json.dumps({"beat_types": ["tension_point", "payoff_beat",
                                                 "aftermath", "hook"]}),))
        assert polarity.craft_checks(_ctx(conn, 4, beats=hot)) == []
        conn.execute("UPDATE pacing_state SET notes=? WHERE chapter=3",
                     (json.dumps({"beat_types": ["tension_point", "payoff_beat", "hook"]}),))
        assert [i["check"] for i in polarity.craft_checks(_ctx(conn, 4, beats=hot))] == [
            "aftermath"]

    def test_aftermath_falls_back_to_planned_beats_of_previous_chapter(self, conn):
        conn.execute("INSERT INTO beats(id, chapter, seq, beat_type, summary)"
                     " VALUES('b1', 6, 1, 'tension_point', '当众挑衅')")
        conn.execute("INSERT INTO beats(id, chapter, seq, beat_type, summary)"
                     " VALUES('b2', 6, 2, 'payoff_beat', '当众打脸')")
        hot = [_beat("tension_point"), _beat("hook")]
        assert [i["check"] for i in polarity.craft_checks(_ctx(conn, 7, beats=hot))] == [
            "aftermath"]
        assert polarity.craft_checks(_ctx(conn, 8, beats=hot)) == []
        # 只有一个 payoff_beat 的计划章：小爽点，不提示
        conn.execute("INSERT INTO beats(id, chapter, seq, beat_type, summary)"
                     " VALUES('b3', 8, 1, 'payoff_beat', '小胜')")
        assert polarity.craft_checks(_ctx(conn, 9, beats=hot)) == []

    def test_contract_polarity_is_read_from_db_without_plan_context(self, conn):
        """治理端点只给 draft/beats：从 chapter_plan_versions 读取计划转向。"""
        _seed(conn, [(1, "+", 2), (2, "+", 2), (3, "+", 2)])
        conn.execute("INSERT INTO chapter_plan_versions(id, chapter, version, payload_json,"
                     " status) VALUES('pv4', 4, 1, '{\"polarity\": \"正\"}', 'active')")
        conn.execute("INSERT INTO chapter_cards(id, chapter, goal, current_plan_version_id)"
                     " VALUES('cc4', 4, '目标', 'pv4')")
        issues = polarity.craft_checks(_ctx(conn, 4))
        assert [i["check"] for i in issues] == ["polarity_streak"]

    def test_old_database_and_missing_conn_are_silent(self):
        legacy = sqlite3.connect(":memory:")
        legacy.row_factory = sqlite3.Row
        assert polarity.craft_checks(_ctx(legacy, 4, contract={"polarity": "-"})) == []
        assert polarity.craft_checks(_ctx(None, 4)) == []

    def test_craft_check_skill_surfaces_warns_only(self, conn):
        from novelforge.control_plane.skill_base import SkillContext
        from novelforge.skills.craft_check_skill import CraftCheckSkill

        beats = [_beat("setup", axis="平静→危机"), _beat("hook", axis="悬念↑")]
        fresh = SkillContext(project_id="p", target_chapter=4, mode="human_gate",
                             as_of_chapter=3, budget=None, llm=None, conn=conn,
                             workspace={"beats": beats, "draft_text": "短稿", "proposals": []})
        assert CraftCheckSkill().run(fresh).ok
        assert not [i for i in fresh.workspace["craft_issues"]
                    if i["category"].startswith("craft.polarity")]

        _seed(conn, [(1, "-", 2), (2, "-", 2), (3, "-", 2)])
        result = CraftCheckSkill().run(fresh)
        found = [i for i in fresh.workspace["craft_issues"] if i["check"] == "polarity_turn"]
        assert result.ok and len(found) == 1 and found[0]["severity"] == "warn"
        assert found[0]["repair_scope"] == "structural" and found[0]["fix"]


# ── 5. draft_brief ──────────────────────────────────────────────────────────

class TestDraftBrief:
    def test_empty_when_irrelevant(self, conn):
        assert polarity.draft_brief(conn, 4, {}, language="zh-CN", cfg=None) == ""
        _seed(conn, [(1, "-", 2), (2, "-", 2), (3, "+", 2)])
        assert polarity.draft_brief(conn, 4, {}, language="zh-CN", cfg=None) == ""
        assert polarity.draft_brief(conn, 1, {}, language="zh-CN", cfg=None) == ""

    def test_low_streak_requires_turn_and_stashes_settings(self, conn):
        _seed(conn, [(1, "-", 2), (2, "-", 2), (3, "-", 2)])
        workspace: dict = {}
        brief = polarity.draft_brief(conn, 4, workspace, language="zh-CN",
                                     cfg=NovelForgeConfig())
        assert brief.startswith("## 节奏要求\n")
        assert "已连续 3 章低谷" in brief and "必须安排转机" in brief
        assert workspace[polarity.SETTINGS_KEY] == {
            "enabled": True, "max_same_polarity_streak": 3, "max_low_streak": 3}

    def test_does_not_repeat_the_cursor_hint(self, conn):
        _seed(conn, [(1, "-", 2), (2, "-", 2), (3, "-", 2)])
        ws = {"pacing_state": PacingState(low_streak=3)}
        assert polarity.draft_brief(conn, 4, ws, language="zh-CN", cfg=None) == ""
        stale = {"pacing_state": PacingState(low_streak=5)}   # 游标与本章历史不一致
        assert "必须安排转机" in polarity.draft_brief(conn, 4, stale, language="zh-CN",
                                                      cfg=None)

    def test_contract_aware_lines(self, conn):
        _seed(conn, [(1, "-", 2), (2, "-", 2), (3, "-", 2)])

        def brief_for(planned):
            ws = {"plan_context": PlanContext(target_chapter=4,
                                              current_contract={"polarity": planned})}
            return polarity.draft_brief(conn, 4, ws, language="zh-CN", cfg=None)

        assert brief_for("+") == ""                       # 契约已安排转机
        assert "契约仍计划转向 -" in brief_for("-")         # 不与契约硬冲突，建议 ±

    def test_positive_streak(self, conn):
        _seed(conn, [(1, "+", 2), (2, "+", 2), (3, "+", 2)])
        brief = polarity.draft_brief(conn, 4, {}, language="zh-CN", cfg=None)
        assert "已连续 3 章顺境" in brief and "反向转折" in brief

    def test_aftermath_length_scales_with_climax(self, conn):
        _seed(conn, [(3, "+", 4)])
        # 单个 payoff_beat 是小爽点：不要求余韵（避免爽文几乎每章都被要求开场平复）
        assert polarity.draft_brief(conn, 4, {}, language="zh-CN", cfg=None) == ""
        conn.execute("UPDATE pacing_state SET tension_level=5, notes=? WHERE chapter=3",
                     (json.dumps({"beat_types": ["setup", "tension_point", "payoff_beat",
                                                 "hook"]}),))
        small = polarity.draft_brief(conn, 4, {}, language="zh-CN", cfg=None)
        assert "余韵" in small and "一小段" in small and "大高潮" in small
        conn.execute("UPDATE pacing_state SET notes=? WHERE chapter=3",
                     (json.dumps({"beat_types": ["tension_point", "payoff_beat",
                                                 "tension_point", "payoff_beat"]}),))
        big = polarity.draft_brief(conn, 4, {}, language="zh-CN", cfg=None)
        assert "约前三分之一" in big
        ws = {"plan_context": PlanContext(target_chapter=4, current_contract={
            "beats": [_beat("aftermath"), _beat("hook")]})}
        assert polarity.draft_brief(conn, 4, ws, language="zh-CN", cfg=None) == ""
        # 上一章在最后一个 payoff_beat 之后已安排余韵：不再要求
        conn.execute("UPDATE pacing_state SET notes=? WHERE chapter=3",
                     (json.dumps({"beat_types": ["tension_point", "payoff_beat",
                                                 "aftermath", "hook"]}),))
        assert polarity.draft_brief(conn, 4, {}, language="zh-CN", cfg=None) == ""

    def test_cursor_hint_with_other_threshold_does_not_silence_the_brief(self, conn):
        _seed(conn, [(1, "-", 2), (2, "-", 2), (3, "-", 2)])
        cfg = NovelForgeConfig()
        cfg.polarity.max_low_streak = 5
        # 游标按默认阈值 3 给出了"需要转机"，但本项目阈值是 5：两者口径不同，不能互相抵消
        ws = {"pacing_state": PacingState(low_streak=3)}
        brief = polarity.draft_brief(conn, 4, ws, language="zh-CN", cfg=cfg)
        assert "已连续 3 章低谷" in brief          # 同向上限 3 仍要求转向
        same = {"pacing_state": PacingState(low_streak=3, max_low_streak=3)}
        assert polarity.draft_brief(conn, 4, same, language="zh-CN", cfg=None) == ""

    def test_english_positive_streak_brief(self, conn):
        _seed(conn, [(1, "+", 2), (2, "+", 2), (3, "+", 2)])

        def brief_for(planned):
            ws = {"plan_context": PlanContext(target_chapter=4,
                                              current_contract={"polarity": planned})}
            return polarity.draft_brief(conn, 4, ws, language="en-US", cfg=None)

        free = brief_for(None)
        assert free.startswith("## Pacing requirements\n")
        assert "turn the other way this chapter" in free
        planned_win = brief_for("+")
        assert "the contract plans another win" in planned_win
        assert brief_for("-") == ""
        for text in (free, planned_win):
            assert not _CJK.search(text)

    def test_english_brief(self, conn):
        _seed(conn, [(1, "-", 2), (2, "-", 2), (3, "-", 5)])
        brief = polarity.draft_brief(conn, 4, {}, language="en-US", cfg=None)
        assert brief.startswith("## Pacing requirements\n")
        assert "must turn toward the goal" in brief and "aftermath" in brief
        assert not _CJK.search(brief)

    def test_disabled(self, conn):
        _seed(conn, [(1, "-", 2), (2, "-", 2), (3, "-", 2)])
        cfg = NovelForgeConfig()
        cfg.polarity.enabled = False
        workspace: dict = {}
        assert polarity.draft_brief(conn, 4, workspace, language="zh-CN", cfg=cfg) == ""
        assert workspace[polarity.SETTINGS_KEY]["enabled"] is False

    @pytest.mark.parametrize("language", ["zh-CN", "en-US"])
    @pytest.mark.parametrize("planned", [None, "-", "+"])
    def test_no_routing_markers(self, conn, language, planned):
        _seed(conn, [(1, "-", 2), (2, "-", 2), (3, "-", 5), (5, "+", 2), (6, "+", 2),
                     (7, "+", 5)])
        for chapter in (4, 8):
            ws = {"plan_context": PlanContext(target_chapter=chapter,
                                              current_contract={"polarity": planned})}
            brief = polarity.draft_brief(conn, chapter, ws, language=language, cfg=None)
            assert brief or planned is not None
            assert not [m for m in _ROUTING_MARKERS if m in brief]


# ── 6. 节拍游标（pacing.py）与英文提示 ──────────────────────────────────────

class TestPacing:
    def test_aftermath_is_a_cooldown(self, conn):
        pc = PacingController()
        pc.update(1, [_beat("tension_point")], 2000, conn)
        assert pc.update(2, [_beat("payoff_beat")], 2000, conn).recent_high_streak == 2
        state = pc.update(3, [_beat("aftermath"), _beat("tension_point")], 2000, conn)
        assert state.recent_high_streak == 0

    def test_state_reads_new_columns_and_threshold(self, conn):
        conn.execute("INSERT INTO pacing_cursor(id, low_streak, polarity_streak, last_polarity)"
                     " VALUES(1, 2, 2, '-')")
        state = PacingController().get_state(conn)
        assert (state.low_streak, state.last_polarity, state.needs_turn) == (2, "-", False)
        assert PacingController().get_state(conn, max_low_streak=2).needs_turn

    def test_legacy_cursor_without_new_columns(self):
        legacy = sqlite3.connect(":memory:")
        legacy.row_factory = sqlite3.Row
        legacy.execute("CREATE TABLE pacing_cursor (id INTEGER PRIMARY KEY,"
                       " chapters_since_big_payoff INTEGER, kchars_since_small_payoff REAL,"
                       " buildup INTEGER, recent_high_streak INTEGER, updated_at TEXT)")
        legacy.execute("INSERT INTO pacing_cursor VALUES(1, 4, 1.5, 3, 1, '')")
        state = PacingController().get_state(legacy)
        assert state.chapters_since_big_payoff == 4
        assert (state.low_streak, state.polarity_streak, state.last_polarity) == (0, 0, None)

    def test_zh_hint_adds_turn_requirement(self):
        pc = PacingController()
        assert "转机" not in pc.recommend_beat_hint(PacingState(low_streak=2))
        hint = pc.recommend_beat_hint(PacingState(low_streak=3, recent_high_streak=3))
        first, second = hint.split("\n")
        assert "已连续 3 章低谷" in first and "转机" in first
        assert "回落" in second

    def test_english_hint(self):
        from novelforge.control_plane.orchestrator import _profile_pacing_hint
        pc = PacingController()
        en = {"content_language": "en-US", "quality_rules": {}}
        hint = _profile_pacing_hint(en, pc, PacingState(low_streak=3))
        assert "must turn toward the goal" in hint and not _CJK.search(hint)
        both = _profile_pacing_hint(en, pc, PacingState(low_streak=3, recent_high_streak=3))
        assert both.count("Pacing note:") == 2 and "breathe" in both
        # 旧口径保持不变
        assert _profile_pacing_hint(en, pc, PacingState()) == ""
        progress = {"content_language": "en-US",
                    "quality_rules": {"requires_progress_event": True}}
        assert "progress event" in _profile_pacing_hint(
            progress, pc, PacingState(kchars_since_small_payoff=4.0))
        zh = _profile_pacing_hint({"content_language": "zh-CN"}, pc, PacingState(low_streak=3))
        assert "转机" in zh


# ── 7. API 与端到端 ──────────────────────────────────────────────────────────

@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_DATA", str(tmp_path))
    import novelforge.app.autopilot_manager as ap
    import novelforge.app.deps as deps
    monkeypatch.setattr(deps, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(deps, "_REGISTRY_FILE", tmp_path / "projects.json")
    deps._registry = None
    ap._manager = None
    yield tmp_path
    deps._registry = None
    ap._manager = None


@pytest.fixture
def client(tmp_data):
    from novelforge.app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def project(client):
    resp = client.post("/v1/projects", json={"name": "F3", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


def _open(project_id):
    from novelforge.app.deps import get_registry
    return get_registry().open_conn(project_id)


def _orch(project_id, factory, *, max_low_streak=None):
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway
    from novelforge.control_plane.orchestrator import Orchestrator
    from novelforge.control_plane.skill_registry import SkillRegistry
    from novelforge.skills import register_default_skills

    fake = FakeProvider(factory=factory)
    gw = LLMGateway(fake, BudgetLedger(max_tokens=10_000_000, max_usd=100.0,
                                       max_revise_rounds=100))
    reg = SkillRegistry()
    register_default_skills(reg)
    cfg = NovelForgeConfig(project_id=project_id)
    cfg.provider.provider = "fake"
    cfg.recall.enable_summaries = False
    cfg.settle.enabled = False
    if max_low_streak is not None:
        cfg.polarity.max_low_streak = max_low_streak
    return Orchestrator(gw, reg, cfg), fake


def _factory(beats: list[dict], body: str):
    beats_json = json.dumps(beats, ensure_ascii=False)

    def factory(messages, model="", temperature=1.0):
        user = str(messages[-1].content) if messages else ""
        if "规划任务" in user:
            return beats_json
        if "本章任务" in user:
            return f"```draft\n{body}\n```\n```proposals\n[]\n```"
        return "[]"
    return factory


def _user(call) -> str:
    return str(call["messages"][-1].content)


class TestApiAndPipeline:
    def test_pacing_endpoint(self, client, project):
        resp = client.get(f"/v1/{project}/pacing")
        assert resp.status_code == 200
        body = resp.json()
        assert body["chapters"] == [] and body["cursor"]["low_streak"] == 0
        assert body["max_low_streak"] == 3

        conn = _open(project)
        try:
            for ch in (2, 1, 3):
                _settle(conn, ch, "-", beats=[_beat("payoff_beat"), _beat("hook")])
        finally:
            conn.close()
        body = client.get(f"/v1/{project}/pacing").json()
        assert [c["chapter"] for c in body["chapters"]] == [1, 2, 3]
        assert body["chapters"][0]["polarity"] == "-"
        assert body["chapters"][0]["detail"]["beat_types"] == ["payoff_beat", "hook"]
        assert body["cursor"]["low_streak"] == 3 and body["cursor"]["needs_turn"] is True

    def test_pacing_endpoint_uses_project_threshold(self, client, project, monkeypatch):
        import novelforge.control_plane.llm.runtime as runtime

        real = runtime.build_project_config

        def patched(*args, **kwargs):
            cfg = real(*args, **kwargs)
            cfg.polarity.max_low_streak = 5
            return cfg

        monkeypatch.setattr(runtime, "build_project_config", patched)
        conn = _open(project)
        try:
            for ch in (1, 2, 3):
                _settle(conn, ch, "-")
        finally:
            conn.close()
        body = client.get(f"/v1/{project}/pacing").json()
        assert body["max_low_streak"] == 5
        assert body["cursor"]["low_streak"] == 3 and body["cursor"]["needs_turn"] is False

    def test_settle_runs_in_pipeline_and_is_idempotent(self, client, project):
        beats = [
            {"beat_type": "setup", "summary": "林动被困在血色秘境", "value_axis": "平静→危机"},
            {"beat_type": "turn", "summary": "林动识破长老的阴谋", "value_axis": "绝境→翻盘"},
            {"beat_type": "hook", "summary": "神秘人现身", "value_axis": "悬念↑"},
        ]
        body = ("林动被困在血色秘境。" + "风声呜咽，石壁渗血。" * 40
                + "林动识破长老的阴谋。" + "众人哗然。" * 40 + "神秘人现身。")
        orch, _ = _orch(project, _factory(beats, body))
        conn = _open(project)
        try:
            for _ in range(2):                         # 同章重写两次
                outcome = orch.generate_chapter(1, conn, chapter_goal="破局")
                assert outcome.ok and not outcome.state_degraded, outcome.error
            rows = conn.execute("SELECT * FROM pacing_state").fetchall()
            assert len(rows) == 1
            assert (rows[0]["polarity"], rows[0]["turning_point"]) == ("+", "revelation")
            detail = json.loads(conn.execute(
                "SELECT detail_json FROM pipeline_run WHERE run_id=?", (outcome.run_id,)
            ).fetchone()[0])
            report = detail["extensions"]["polarity"]
            assert report["polarity"] == "+" and report["source"] == "beats"
            assert report["polarity_streak"] == 1 and report["last_polarity"] == "+"
        finally:
            conn.close()

    def test_brief_hint_and_warn_reach_the_prompts_in_order(self, client, project):
        conn = _open(project)
        try:
            _seed(conn, [(1, "-", 2), (2, "-", 2), (3, "-", 2)])
            PacingController().recompute_polarity_streaks(conn)
            conn.execute("INSERT INTO chapter_plan_versions(id, chapter, version,"
                         " payload_json, status) VALUES('pv4', 4, 1,"
                         " '{\"polarity\": \"-\"}', 'active')")
            conn.execute("INSERT INTO chapter_cards(id, chapter, goal, current_plan_version_id)"
                         " VALUES('cc4', 4, '林动继续隐忍', 'pv4')")
            conn.commit()
            beats = [
                {"beat_type": "setup", "summary": "开场", "value_axis": "平静→波澜"},
                {"beat_type": "turn", "summary": "转折", "value_axis": "守→攻"},
                {"beat_type": "hook", "summary": "章末悬念", "value_axis": "悬念↑"},
            ]
            orch, fake = _orch(project, _factory(beats, "平稳正文。" * 200))
            outcome = orch.generate_chapter(4, conn, chapter_goal="测试")
            assert outcome.ok, outcome.error

            draft = next(c for c in fake.calls if "本章任务" in _user(c))
            text = _user(draft)
            pos = text.index("## 节奏要求")
            assert pos >= draft["cache_hint"].user_prefix_chars      # 不进稳定前缀
            assert text.index("## 本章硬契约") < pos < text.index("## 本章任务")
            assert "契约仍计划转向 -" in text[pos:text.index("## 本章任务")]
            assert "节奏提示：已连续 3 章低谷" in text               # 游标提示进入章节目标

            planner = next(c for c in fake.calls if "规划任务" in _user(c))
            ptext = _user(planner)
            assert planner["cache_hint"].user_prefix_chars <= ptext.index("## 节奏要求")
            assert ptext.index("## 节奏要求") < ptext.index("## 规划任务")

            turns = [i for i in outcome.issues if i.get("check") == "polarity_turn"]
            assert len(turns) == 1 and turns[0]["severity"] == "warn"
        finally:
            conn.close()

    def test_configured_low_streak_threshold_reaches_the_cursor_hint(self, client, project):
        """cfg.polarity.max_low_streak=5：3 章低谷时游标提示不触发，节奏要求块照常给出。"""
        conn = _open(project)
        try:
            _seed(conn, [(1, "-", 2), (2, "-", 2), (3, "-", 2)])
            PacingController().recompute_polarity_streaks(conn)
            conn.commit()
            beats = [
                {"beat_type": "setup", "summary": "开场", "value_axis": "平静→波澜"},
                {"beat_type": "hook", "summary": "章末悬念", "value_axis": "悬念↑"},
            ]
            orch, fake = _orch(project, _factory(beats, "平稳正文。" * 200),
                               max_low_streak=5)
            outcome = orch.generate_chapter(4, conn, chapter_goal="测试")
            assert outcome.ok, outcome.error
            text = _user(next(c for c in fake.calls if "本章任务" in _user(c)))
            assert "节奏提示：已连续" not in text
            brief = text[text.index("## 节奏要求"):text.index("## 本章任务")]
            assert "已连续 3 章低谷" in brief and "必须安排转机" in brief
            assert not [i for i in outcome.issues if i.get("check") == "polarity_turn"]
        finally:
            conn.close()
