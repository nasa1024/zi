"""v22/F2 "天降外挂"拦截器：五项确定性检查 + 治理端点上下文 + 端到端。全部离线。"""
from __future__ import annotations

import json
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from novelforge.config import NovelForgeConfig, PlausibilityConfig
from novelforge.control_plane.skill_base import SkillContext
from novelforge.craft import deus_ex_machina as dem
from novelforge.db.connection import index_draft_fts, init_db_from_conn, set_meta
from novelforge.planning.context import PlanContext

_CJK = re.compile(r"[\u3400-\u9fff]")

PAYOFF_BEATS = [
    {"beat_type": "setup", "summary": "开场", "value_axis": "平静→波澜"},
    {"beat_type": "turn", "summary": "围攻", "value_axis": "守→危"},
    {"beat_type": "payoff_beat", "summary": "绝地反击", "value_axis": "反击"},
    {"beat_type": "hook", "summary": "章末悬念", "value_axis": "悬念↑"},
]
PLAIN_BEATS = [
    {"beat_type": "setup", "summary": "开场", "value_axis": "平静→波澜"},
    {"beat_type": "turn", "summary": "转折", "value_axis": "守→攻"},
    {"beat_type": "hook", "summary": "章末悬念", "value_axis": "悬念↑"},
]
FILLER = "林凡站在山门前，望着翻涌的云海，心里盘算着下一步。"
CLIMAX = "危急关头，玄天老祖从天而降，一掌击退魔头。"
HOOK_TAIL = "夜色渐深，山风卷起落叶，远处传来几声钟响。" * 3


# ── fixtures / helpers ───────────────────────────────────────────────────────

@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db_from_conn(c)
    yield c
    c.close()


def _ctx(conn, chapter, draft, *, beats=None, proposals=None, workspace=None):
    ws = {
        "draft_text": draft,
        "beats": PAYOFF_BEATS if beats is None else beats,
        "proposals": proposals or [],
        "plan_context": PlanContext(target_chapter=chapter),
        "writing_profile": {"content_language": "zh-CN"},
    }
    ws.update(workspace or {})
    return SkillContext(
        project_id="p", target_chapter=chapter, mode="human_gate",
        as_of_chapter=chapter - 1, budget=None, llm=None, conn=conn, workspace=ws,
    )


def _draft(climax: str, *, filler_times: int = 10, tail: str = HOOK_TAIL) -> str:
    """铺垫 → 高潮 → 章末钩子；climax 约在 72%，同时落在 payoff 窗口与"末尾 40% 去钩子段"里。"""
    return FILLER * filler_times + climax + tail


def _entity(conn, eid, name, etype="character", first=None, aliases=()):
    conn.execute(
        "INSERT INTO entities(id, canonical_name, entity_type, first_appear_chapter)"
        " VALUES(?,?,?,?)", (eid, name, etype, first),
    )
    for i, alias in enumerate(aliases):
        conn.execute(
            "INSERT INTO entity_aliases(id, entity_id, alias) VALUES(?,?,?)",
            (f"{eid}_a{i}", eid, alias),
        )


def _gimmick(conn, gid, name, valid_from, cost=None):
    conn.execute(
        "INSERT INTO gimmick_rules(id, gimmick_name, cost_json, valid_from_chapter)"
        " VALUES(?,?,?,?)",
        (gid, name, json.dumps(cost, ensure_ascii=False) if cost is not None else None,
         valid_from),
    )


def _usage(conn, uid, gid, chapter, user="e_lin"):
    if conn.execute("SELECT 1 FROM entities WHERE id=?", (user,)).fetchone() is None:
        _entity(conn, user, "林凡", first=1)
    conn.execute(
        "INSERT INTO gimmick_usage_log(id, gimmick_id, user_entity_id, use_chapter)"
        " VALUES(?,?,?,?)", (uid, gid, user, chapter),
    )


def _index_chapter(conn, chapter, text):
    """模拟已落袋的早前章节：draft_index + 真实分词的 drafts_fts。"""
    draft_id = f"d{chapter}"
    conn.execute(
        "INSERT INTO draft_index(id, chapter, revision_round, file_path, sha256)"
        " VALUES(?,?,0,?,?)", (draft_id, chapter, f"l0/{draft_id}.md", "sha"),
    )
    index_draft_fts(conn, draft_id, chapter, text)


def _volume(conn, start, end, vid="v1", no=1):
    conn.execute(
        "INSERT INTO volumes(id, volume_no, title, start_chapter, end_chapter)"
        " VALUES(?,?,?,?,?)", (vid, no, f"卷{no}", start, end),
    )


def _checks(issues, name):
    return [i for i in issues if i["check"] == name]


def _assert_shape(issue, draft):
    assert issue["severity"] == "warn"
    assert issue["category"] == f"craft.deus_ex_machina.{issue['check']}"
    assert issue["fix"]
    assert issue["span"] and issue["span"] in draft


# ── 解决章判定与高潮段 ────────────────────────────────────────────────────────

class TestResolvingRegion:
    def test_payoff_beat_window(self):
        beats = [{"beat_type": "setup"}, {"beat_type": "payoff_beat"},
                 {"beat_type": "turn"}, {"beat_type": "hook"}]
        # payoff 是第 2/4 个 beat → [25%−10%, 50%+10%)。
        assert dem._resolving_regions(1000, beats, 0.4) == [(150, 600)]
        assert dem._resolving_regions(1000, PAYOFF_BEATS, 0.4) == [(400, 850)]

    def test_tail_without_payoff_drops_hook_segment(self):
        assert dem._resolving_regions(1000, PLAIN_BEATS, 0.4) == [(600, 800)]
        no_hook = [{"beat_type": "setup"}, {"beat_type": "turn"}]
        assert dem._resolving_regions(1000, no_hook, 0.4) == [(600, 1000)]

    def test_contract_must_resolve_marks_resolving(self):
        assert dem._is_resolving({"must_resolve": ["击退魔头"]}, PLAIN_BEATS)
        assert not dem._is_resolving({"must_resolve": [""]}, PLAIN_BEATS)
        assert dem._is_resolving({"beats": [{"beat_type": "payoff_beat"}]}, [])
        assert not dem._is_resolving({}, PLAIN_BEATS)

    def test_non_resolving_chapter_skips_climax_checks(self, conn):
        _entity(conn, "e2", "玄天老祖", first=5)
        issues = dem.craft_checks(_ctx(conn, 5, _draft(CLIMAX), beats=PLAIN_BEATS))
        assert issues == []

    def test_contract_must_resolve_without_payoff_beat(self, conn):
        _entity(conn, "e2", "玄天老祖", first=5)
        pc = PlanContext(target_chapter=5, current_contract={"must_resolve": ["击退魔头"]})
        ctx = _ctx(conn, 5, _draft(CLIMAX), beats=PLAIN_BEATS, workspace={"plan_context": pc})
        assert len(_checks(dem.craft_checks(ctx), dem.CHECK_NEWCOMER)) == 1


# ── a) 新登场者解决高潮 ──────────────────────────────────────────────────────

class TestNewcomerResolver:
    def test_first_appear_this_chapter_in_climax_warns(self, conn):
        _entity(conn, "e_lin", "林凡", first=1)
        _entity(conn, "e2", "玄天老祖", first=5)
        draft = _draft(CLIMAX)
        issues = dem.craft_checks(_ctx(conn, 5, draft))
        [issue] = _checks(issues, dem.CHECK_NEWCOMER)
        _assert_shape(issue, draft)
        assert "高潮由本章新登场的「玄天老祖」解决（天降外挂）" in issue["detail"]
        assert "首次出场就在高潮段" in issue["detail"]
        assert issue["span"] == CLIMAX
        assert issue["repair_scope"] == "structural"

    def test_known_entity_is_silent(self, conn):
        _entity(conn, "e2", "玄天老祖", first=2)
        assert dem.craft_checks(_ctx(conn, 5, _draft(CLIMAX))) == []

    def test_newcomer_outside_climax_is_not_resolver(self, conn):
        _entity(conn, "e2", "玄天老祖", first=5)
        draft = CLIMAX + FILLER * 12
        assert _checks(dem.craft_checks(_ctx(conn, 5, draft, beats=[
            {"beat_type": "setup"}, {"beat_type": "turn"}, {"beat_type": "turn"},
            {"beat_type": "payoff_beat"}, {"beat_type": "hook"},
        ])), dem.CHECK_NEWCOMER) == []

    def test_alias_mention_and_setup_earlier_in_chapter(self, conn):
        _entity(conn, "e2", "玄天老祖", first=5, aliases=["老祖"])
        draft = "众人提起老祖闭关多年。" + _draft("危急关头，老祖从天而降。")
        [issue] = _checks(dem.craft_checks(_ctx(conn, 5, draft)), dem.CHECK_NEWCOMER)
        assert issue["span"] == "危急关头，老祖从天而降。"
        assert "首次出场就在高潮段" not in issue["detail"]

    def test_null_first_appear_uses_earlier_text(self, conn):
        _entity(conn, "e2", "玄天老祖")
        _index_chapter(conn, 1, "林凡听说玄天老祖已闭关百年。")
        _index_chapter(conn, 2, "林凡继续赶路。")
        assert dem.craft_checks(_ctx(conn, 3, _draft(CLIMAX))) == []

    def test_null_first_appear_without_any_trace_warns(self, conn):
        _entity(conn, "e2", "玄天老祖")
        _index_chapter(conn, 1, "林凡下山。")
        _index_chapter(conn, 2, "林凡继续赶路。")
        assert len(_checks(dem.craft_checks(_ctx(conn, 3, _draft(CLIMAX))),
                           dem.CHECK_NEWCOMER)) == 1

    def test_null_first_appear_unverifiable_history_is_silent(self, conn):
        """早前章节有草稿却没进 FTS：无法证明"从未提及"→ 宁漏勿误报。"""
        _entity(conn, "e2", "玄天老祖")
        _index_chapter(conn, 1, "林凡下山。")
        conn.execute(
            "INSERT INTO draft_index(id, chapter, revision_round, file_path, sha256)"
            " VALUES('d2', 2, 0, 'l0/d2.md', 'sha')"
        )
        assert dem.craft_checks(_ctx(conn, 3, _draft(CLIMAX))) == []

    def test_earlier_fact_counts_as_appearance(self, conn):
        _entity(conn, "e2", "玄天老祖")
        conn.execute(
            "INSERT INTO facts(id, entity_id, fact_type, subject, predicate, object, status,"
            " valid_from_chapter, current_revision_id)"
            " VALUES('f1','e2','character_trait','玄天老祖','身份','林家先祖','canon',2,'r1')"
        )
        assert dem.craft_checks(_ctx(conn, 5, _draft(CLIMAX))) == []

    def test_seed_fact_at_chapter_zero_is_not_an_appearance(self, conn):
        _entity(conn, "e2", "玄天老祖")
        conn.execute(
            "INSERT INTO facts(id, entity_id, fact_type, subject, predicate, object, status,"
            " valid_from_chapter, current_revision_id)"
            " VALUES('f1','e2','character_trait','玄天老祖','身份','林家先祖','canon',0,'r1')"
        )
        _index_chapter(conn, 1, "林凡下山。")
        assert len(_checks(dem.craft_checks(_ctx(conn, 2, _draft(CLIMAX))),
                           dem.CHECK_NEWCOMER)) == 1

    def test_proposal_introduced_newcomer(self, conn):
        _index_chapter(conn, 1, "林凡下山。")
        _index_chapter(conn, 2, "林凡继续赶路。")
        draft = _draft("危急关头，萧玄一剑斩落魔头首级。黑衣人四散奔逃。")
        proposals = [
            {"op": "add", "fact_type": "event", "entity": "萧玄",
             "new": {"subject": "萧玄", "predicate": "击杀", "object": "魔头"}},
            {"op": "add", "fact_type": "event", "entity": "黑衣人",
             "new": {"subject": "黑衣人", "predicate": "逃走", "object": "山林"}},
            {"op": "add", "fact_type": "event", "entity": "林凡",
             "new": {"subject": "林凡", "predicate": "获救", "object": "山门"}},
        ]
        issues = dem.craft_checks(_ctx(conn, 3, draft, proposals=proposals))
        # 林凡早前出现过；黑衣人是无名龙套；只剩真正的新面孔萧玄。
        [issue] = _checks(issues, dem.CHECK_NEWCOMER)
        assert "「萧玄」" in issue["detail"] and "该实体此前章节从未出现" in issue["detail"]
        assert issue["span"] == "危急关头，萧玄一剑斩落魔头首级。"

    def test_newcomer_in_chapter_end_hook_is_not_a_resolver(self, conn):
        _entity(conn, "e7", "厉千秋", first=5)
        hook = "远处，一道身影缓缓走来——血魔宗少主，厉千秋。"
        draft = _draft("林凡咬牙一剑，击退魔头。", tail=HOOK_TAIL + hook)
        assert dem.craft_checks(_ctx(conn, 5, draft)) == []

    def test_first_chapter_is_exempt(self, conn):
        _entity(conn, "e2", "玄天老祖", first=1)
        assert dem.craft_checks(_ctx(conn, 1, _draft(CLIMAX))) == []

    def test_cap_three(self, conn):
        names = ["甲一", "乙二", "丙三", "丁四"]
        for i, name in enumerate(names):
            _entity(conn, f"n{i}", name, first=5)
        draft = _draft("危急关头，" + "、".join(names) + "联手击退魔头。")
        assert len(_checks(dem.craft_checks(_ctx(conn, 5, draft)), dem.CHECK_NEWCOMER)) == 3


# ── b) 未经展示的能力 ────────────────────────────────────────────────────────

class TestUndemonstratedAbility:
    def test_ability_defined_this_chapter_warns(self, conn):
        _gimmick(conn, "g1", "血遁术", 5)
        draft = _draft("林凡施展血遁术，逃出生天。")
        [issue] = _checks(dem.craft_checks(_ctx(conn, 5, draft)), dem.CHECK_UNDEMONSTRATED)
        _assert_shape(issue, draft)
        assert issue["detail"].startswith("能力未经铺垫即用于解决冲突：「血遁术」本章才首次设定")

    def test_old_rule_without_usage_or_mention_warns(self, conn):
        _gimmick(conn, "g1", "血遁术", 0)
        _index_chapter(conn, 1, "林凡下山。")
        [issue] = _checks(dem.craft_checks(_ctx(conn, 2, _draft("林凡施展血遁术，逃出生天。"))),
                          dem.CHECK_UNDEMONSTRATED)
        assert "此前从未展示或使用过" in issue["detail"]

    def test_prior_usage_log_counts_as_demonstrated(self, conn):
        _gimmick(conn, "g1", "血遁术", 0)
        _usage(conn, "u1", "g1", 2)
        _index_chapter(conn, 1, "林凡下山。")
        assert dem.craft_checks(_ctx(conn, 5, _draft("林凡施展血遁术，逃出生天。"))) == []

    def test_prior_text_mention_counts_as_setup(self, conn):
        _gimmick(conn, "g1", "血遁术", 5)
        _index_chapter(conn, 3, "古籍记载，血遁术可燃血千里遁走。")
        assert dem.craft_checks(_ctx(conn, 5, _draft("林凡施展血遁术，逃出生天。"))) == []

    def test_rule_proposal_without_db_still_warns(self):
        proposals = [{"op": "add", "fact_type": "power_system", "entity": "林凡",
                      "new": {"facet": "gimmick_rule", "gimmick_name": "逆天改命"}}]
        draft = _draft("林凡祭出逆天改命，扭转乾坤。")
        ctx = _ctx(None, 5, draft, proposals=proposals)
        [issue] = _checks(dem.craft_checks(ctx), dem.CHECK_UNDEMONSTRATED)
        assert "「逆天改命」本章才首次设定" in issue["detail"]

    def test_soft_magic_softens_wording_but_still_warns(self, conn):
        _gimmick(conn, "g1", "血遁术", 5)
        cfg = NovelForgeConfig()
        cfg.plausibility.soft_magic = True
        ctx = _ctx(conn, 5, _draft("林凡施展血遁术，逃出生天。"), workspace={"cfg": cfg})
        [issue] = _checks(dem.craft_checks(ctx), dem.CHECK_UNDEMONSTRATED)
        assert issue["severity"] == "warn"
        assert issue["detail"].startswith("提示：「血遁术」")
        assert "软魔法" in issue["detail"]

    def test_gimmick_not_reported_as_entity_newcomer(self, conn):
        _entity(conn, "e9", "血遁术", etype="concept", first=5)
        _gimmick(conn, "g1", "血遁术", 5)
        issues = dem.craft_checks(_ctx(conn, 5, _draft("林凡施展血遁术，逃出生天。")))
        assert [i["check"] for i in issues] == [dem.CHECK_UNDEMONSTRATED]


# ── c) 代价未付 ──────────────────────────────────────────────────────────────

class TestUnpaidCost:
    def test_cost_not_paid_in_text_warns(self, conn):
        _gimmick(conn, "g1", "血遁术", 0, cost={"寿元": 10})
        draft = "林凡施展血遁术，逃出生天。" + FILLER
        [issue] = dem.craft_checks(_ctx(conn, 1, draft, beats=PLAIN_BEATS))
        _assert_shape(issue, draft)
        assert issue["check"] == dem.CHECK_UNPAID_COST
        assert issue["detail"] == "代价未付：「血遁术」的使用代价（寿元）未在正文兑现。"
        assert issue["span"] == "林凡施展血遁术，逃出生天。"
        assert issue["repair_scope"] == "local"

    def test_cost_keyword_in_text_is_paid(self, conn):
        _gimmick(conn, "g1", "血遁术", 0, cost=["消耗十年寿元"])
        draft = "林凡施展血遁术，逃出生天，鬓角却白了一片，折损了寿元。"
        assert dem.craft_checks(_ctx(conn, 1, draft, beats=PLAIN_BEATS)) == []

    def test_paid_cost_proposal_is_paid(self, conn):
        _gimmick(conn, "g1", "血遁术", 0, cost={"寿元": 10})
        proposals = [{"op": "add", "fact_type": "power_system", "entity": "林凡",
                      "new": {"facet": "gimmick_use", "gimmick_name": "血遁术",
                              "paid_cost_json": {"寿元": 10}}}]
        draft = "林凡施展血遁术，逃出生天。"
        assert dem.craft_checks(_ctx(conn, 1, draft, beats=PLAIN_BEATS,
                                     proposals=proposals)) == []

    def test_use_proposal_without_paid_cost_warns(self, conn):
        _gimmick(conn, "g1", "血遁术", 0, cost={"type": "精血", "amount": "三成"})
        proposals = [{"op": "add", "fact_type": "power_system", "entity": "林凡",
                      "new": {"facet": "gimmick_use", "gimmick_name": "血遁术"},
                      "evidence_refs": [{"quote": "一道血光遁出千里"}]}]
        draft = "林凡掐诀，一道血光遁出千里。"
        [issue] = dem.craft_checks(_ctx(conn, 1, draft, beats=PLAIN_BEATS,
                                        proposals=proposals))
        assert "（精血）" in issue["detail"]
        assert issue["span"] == draft

    def test_mention_without_use_is_silent(self, conn):
        _gimmick(conn, "g1", "血遁术", 0, cost={"寿元": 10})
        for draft in ("林凡想起血遁术的传说。", "林凡没有动用血遁术，咬牙硬扛。",
                      "林凡想起血遁术，转身催动青冥剑。"):
            assert dem.craft_checks(_ctx(conn, 1, draft, beats=PLAIN_BEATS)) == []

    def test_cost_without_searchable_keyword_is_silent(self, conn):
        _gimmick(conn, "g1", "血遁术", 0, cost={"amount": 3})
        assert dem.craft_checks(_ctx(conn, 1, "林凡施展血遁术。", beats=PLAIN_BEATS)) == []

    def test_cost_keyword_extraction(self):
        assert dem._cost_keywords({"寿元": 10}) == ["寿元"]
        assert dem._cost_keywords("灵力耗尽") == ["灵力耗尽", "灵力"]
        assert dem._cost_keywords({"type": "精血", "amount": "三成"}) == ["精血"]
        assert dem._cost_keywords(["a memory", "ten years of lifespan"]) == ["memory", "lifespan"]


# ── d) 卷末新人/新设定 ───────────────────────────────────────────────────────

class TestLateNewcomer:
    def test_late_volume_newcomer_warns(self, conn):
        _volume(conn, 1, 20)
        _entity(conn, "e3", "苏晴", first=18)
        draft = "苏晴推门而入，带来了北境的消息。" + FILLER * 5
        [issue] = dem.craft_checks(_ctx(conn, 18, draft, beats=PLAIN_BEATS))
        _assert_shape(issue, draft)
        assert issue["check"] == dem.CHECK_LATE_NEWCOMER
        assert issue["detail"] == "卷末 25% 不宜引入新人物/新设定：「苏晴」在本章（卷进度 89%）才首次出现。"

    def test_mid_volume_newcomer_is_fine(self, conn):
        _volume(conn, 1, 20)
        _entity(conn, "e3", "苏晴", first=10)
        assert dem.craft_checks(_ctx(conn, 10, "苏晴推门而入。" + FILLER, beats=PLAIN_BEATS)) == []

    def test_open_volume_has_no_progress(self, conn):
        _volume(conn, 1, None)
        _entity(conn, "e3", "苏晴", first=18)
        assert dem.craft_checks(_ctx(conn, 18, "苏晴推门而入。" + FILLER, beats=PLAIN_BEATS)) == []

    def test_extras_skipped_and_cap_three(self, conn):
        _volume(conn, 1, 20)
        names = ["苏晴", "柳白", "秦烈", "韩冰"]
        for i, name in enumerate(names):
            _entity(conn, f"n{i}", name, first=19)
        _entity(conn, "x1", "黑衣人", first=19)
        draft = "黑衣人退下后，" + "、".join(names) + "先后赶到。" + FILLER
        issues = dem.craft_checks(_ctx(conn, 19, draft, beats=PLAIN_BEATS))
        assert len(issues) == 3
        assert all(i["check"] == dem.CHECK_LATE_NEWCOMER for i in issues)
        assert not any("黑衣人" in i["detail"] for i in issues)

    def test_not_double_reported_with_newcomer_resolver(self, conn):
        _volume(conn, 1, 20)
        _entity(conn, "e2", "玄天老祖", first=19)
        issues = dem.craft_checks(_ctx(conn, 19, _draft(CLIMAX)))
        assert [i["check"] for i in issues] == [dem.CHECK_NEWCOMER]

    def test_new_gimmick_late_in_volume(self, conn):
        _volume(conn, 1, 20)
        _gimmick(conn, "g1", "天眼通", 19)
        issues = dem.craft_checks(_ctx(conn, 19, "林凡睁开天眼通，看穿迷雾。" + FILLER,
                                       beats=PLAIN_BEATS))
        assert [i["check"] for i in issues] == [dem.CHECK_LATE_NEWCOMER]
        assert "「天眼通」" in issues[0]["detail"]

    def test_volume_progress_matches_orchestrator(self, conn):
        from novelforge.control_plane.orchestrator import _volume_progress
        _volume(conn, 1, 20)
        _volume(conn, 21, None, vid="v2", no=2)
        for chapter in (1, 10, 15, 20):
            span = dem._volume_span(conn, chapter, None)
            assert dem._volume_progress(span, chapter) == _volume_progress(conn, chapter)
        assert dem._volume_span(conn, 25, None) == (21, None)
        assert _volume_progress(conn, 25) is None


# ── e) 先扩展再新增 ──────────────────────────────────────────────────────────

class TestNewAbilityRatio:
    def _seed(self, conn):
        _volume(conn, 1, 30)
        _gimmick(conn, "g_old", "青冥剑意", 0)
        _gimmick(conn, "g_a", "血遁术", 3)
        _gimmick(conn, "g_b", "天眼通", 6)
        _gimmick(conn, "g_new", "九转玄功", 10)

    def test_ratio_exceeded_warns_once(self, conn):
        self._seed(conn)
        draft = "林凡默运九转玄功，周身金光流转。" + FILLER * 3
        issues = dem.craft_checks(_ctx(conn, 10, draft, beats=PLAIN_BEATS))
        [issue] = _checks(issues, dem.CHECK_ABILITY_RATIO)
        _assert_shape(issue, draft)
        assert "本卷已新增 3 项能力/金手指（含本章的「九转玄功」）" in issue["detail"]
        assert "新用法只有 0 次" in issue["detail"]
        assert "用一个 A 带出 BCD" in issue["fix"]

    def test_existing_ability_reuse_keeps_ratio_low(self, conn):
        self._seed(conn)
        for i, chapter in enumerate((2, 4, 7)):
            _usage(conn, f"u{i}", "g_old", chapter)
        draft = "林凡默运九转玄功，周身金光流转。" + FILLER * 3
        assert _checks(dem.craft_checks(_ctx(conn, 10, draft, beats=PLAIN_BEATS)),
                       dem.CHECK_ABILITY_RATIO) == []

    def test_chapter_without_new_ability_is_silent(self, conn):
        self._seed(conn)
        draft = "林凡施展血遁术，逃出生天。" + FILLER * 3
        assert _checks(dem.craft_checks(_ctx(conn, 11, draft, beats=PLAIN_BEATS)),
                       dem.CHECK_ABILITY_RATIO) == []

    def test_opening_chapter_is_exempt(self, conn):
        _volume(conn, 1, 30)
        for i, name in enumerate(("血遁术", "天眼通", "九转玄功")):
            _gimmick(conn, f"g{i}", name, 1)
        draft = "林凡得了血遁术、天眼通与九转玄功三门传承。" + FILLER
        assert dem.craft_checks(_ctx(conn, 1, draft, beats=PLAIN_BEATS)) == []

    def test_threshold_is_configurable(self, conn):
        self._seed(conn)
        ctx = _ctx(conn, 10, "林凡默运九转玄功。" + FILLER * 3, beats=PLAIN_BEATS,
                   workspace={"cfg": PlausibilityConfig(new_ability_ratio_warn=5.0)})
        assert _checks(dem.craft_checks(ctx), dem.CHECK_ABILITY_RATIO) == []


# ── 英文项目 ────────────────────────────────────────────────────────────────

class TestEnglish:
    def _en(self, conn):
        set_meta(conn, "content_language", "en-US")
        return {"writing_profile": {"content_language": "en-US"}}

    def test_newcomer_and_unpaid_cost_in_english(self, conn):
        ws = self._en(conn)
        _entity(conn, "e1", "Mara Venn")
        _entity(conn, "e2", "Old Wen")
        _gimmick(conn, "g1", "Ember Sight", 0, cost={"cost": "a memory"})
        _index_chapter(conn, 1, "Mara Venn's lantern guttered as she studied the Ember Sight.")
        filler = "Mara Venn crossed the bridge and counted the lanterns. " * 8
        climax = "Then Old Wen stepped out of the fog and broke the siege. "
        tail = "Somewhere below, the river kept its slow count of the hours. " * 2
        draft = filler + "Mara used the Ember Sight to find the gate. " + climax + tail
        issues = dem.craft_checks(_ctx(conn, 2, draft, workspace=ws))
        checks = sorted(i["check"] for i in issues)
        assert checks == [dem.CHECK_NEWCOMER, dem.CHECK_UNPAID_COST]
        for issue in issues:
            _assert_shape(issue, draft)
            assert not _CJK.search(issue["detail"] + issue["fix"])
        newcomer = _checks(issues, dem.CHECK_NEWCOMER)[0]
        assert newcomer["detail"].startswith("The climax is resolved by Old Wen")
        assert newcomer["span"] == climax.strip()
        unpaid = _checks(issues, dem.CHECK_UNPAID_COST)[0]
        assert "(memory)" in unpaid["detail"]

    def test_english_cost_paid_and_negated_use(self, conn):
        ws = self._en(conn)
        _gimmick(conn, "g1", "Ember Sight", 0, cost=["a memory"])
        paid = "She used the Ember Sight, and another memory of her mother burned away."
        refused = "She did not use the Ember Sight again."
        for draft in (paid, refused):
            assert dem.craft_checks(_ctx(conn, 1, draft, beats=PLAIN_BEATS, workspace=ws)) == []

    def test_english_soft_magic_and_late_newcomer(self, conn):
        ws = self._en(conn)
        _volume(conn, 1, 10)
        _gimmick(conn, "g1", "Starfall", 9)
        _entity(conn, "e5", "Captain Ilse", first=9)
        draft = ("Captain Ilse arrived with the northern fleet. "
                 + "The harbor bells rang through the cold morning. " * 4
                 + "The sky split as Starfall answered the call. "
                 + "Smoke drifted over the empty quay until dusk. " * 2)
        ws["cfg"] = PlausibilityConfig(soft_magic=True)
        issues = dem.craft_checks(_ctx(conn, 9, draft, workspace=ws))
        assert sorted(i["check"] for i in issues) == [
            dem.CHECK_LATE_NEWCOMER, dem.CHECK_UNDEMONSTRATED,
        ]
        soft = _checks(issues, dem.CHECK_UNDEMONSTRATED)[0]
        assert soft["detail"].startswith("Note: Starfall is first established in this chapter")
        late = _checks(issues, dem.CHECK_LATE_NEWCOMER)[0]
        assert late["detail"].startswith("Avoid new characters or setting elements in the last 25%")
        for issue in issues:
            assert not _CJK.search(issue["detail"] + issue["fix"])


# ── 健壮性：治理端点上下文 / 旧库 / 新项目 / 开关 ──────────────────────────────

class TestRobustness:
    def test_governance_style_ctx_reads_contract_from_db(self, conn):
        """治理端点只给 draft/beats/proposals：契约（must_resolve）按章号从库里读。"""
        from novelforge.skills.craft_check_skill import CraftCheckSkill
        _entity(conn, "e2", "玄天老祖", first=5)
        conn.execute(
            "INSERT INTO chapter_cards(id, chapter, title, must_resolve_json)"
            " VALUES('c5', 5, '第五章', '[\"击退魔头\"]')"
        )
        draft = _draft(CLIMAX)
        ws = {"draft_text": draft, "beats": PLAIN_BEATS, "proposals": []}
        ctx = SkillContext(project_id="p", target_chapter=5, mode="human_gate",
                           as_of_chapter=4, budget=None, llm=None, conn=conn, workspace=ws)
        CraftCheckSkill().run(ctx)
        issues = _checks(ws["craft_issues"], dem.CHECK_NEWCOMER)
        assert len(issues) == 1
        assert issues[0]["severity"] == "warn" and issues[0]["source"] == "craft"

    def test_missing_keys_and_no_conn(self):
        ctx = SkillContext(project_id="p", target_chapter=3, mode="human_gate",
                           as_of_chapter=2, budget=None, llm=None, conn=None,
                           workspace={"draft_text": _draft(CLIMAX)})
        assert dem.craft_checks(ctx) == []
        ctx.workspace = {}
        assert dem.craft_checks(ctx) == []

    def test_old_database_without_tables(self):
        bare = sqlite3.connect(":memory:")
        try:
            ctx = _ctx(bare, 5, _draft(CLIMAX))
            assert dem.craft_checks(ctx) == []
        finally:
            bare.close()

    def test_fresh_project_with_proposals_is_silent(self, conn):
        proposals = [{"op": "add", "fact_type": "event", "entity": "萧玄",
                      "new": {"subject": "萧玄", "predicate": "击杀", "object": "魔头"}}]
        draft = _draft("危急关头，萧玄一剑斩落魔头首级。")
        for chapter in (1, 4):
            assert dem.craft_checks(_ctx(conn, chapter, draft, proposals=proposals)) == []

    def test_disabled_by_config(self, conn):
        _entity(conn, "e2", "玄天老祖", first=5)
        cfg = NovelForgeConfig()
        cfg.plausibility.enabled = False
        assert dem.craft_checks(_ctx(conn, 5, _draft(CLIMAX), workspace={"cfg": cfg})) == []

    def test_idempotent(self, conn):
        _entity(conn, "e2", "玄天老祖", first=5)
        _gimmick(conn, "g1", "血遁术", 5, cost={"寿元": 10})
        ctx = _ctx(conn, 5, _draft(CLIMAX + "林凡施展血遁术，逃出生天。"))
        first = dem.craft_checks(ctx)
        assert len(first) == 3
        assert dem.craft_checks(ctx) == first


# ── 端到端：generate_chapter → outcome.issues（warn，不触发 revise）────────────

@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_DATA", str(tmp_path))
    import novelforge.app.autopilot_manager as autopilot_mod
    import novelforge.app.deps as deps_mod

    monkeypatch.setattr(deps_mod, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(deps_mod, "_REGISTRY_FILE", tmp_path / "projects.json")
    deps_mod._registry = None
    autopilot_mod._manager = None
    yield tmp_path
    deps_mod._registry = None
    autopilot_mod._manager = None


@pytest.fixture
def client(tmp_data):
    from novelforge.app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def project(client):
    resp = client.post("/v1/projects", json={"name": "F2拦截器", "genre": "xuanhuan"})
    assert resp.status_code == 201, resp.text
    return resp.json()["project_id"]


_REVISE_MARKERS = ("一致性问题", "修订补丁任务", "润色补丁任务", "工艺问题")


def test_generate_chapter_reports_deus_ex_machina_as_warn(project):
    from novelforge.app.deps import get_registry
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway
    from novelforge.control_plane.orchestrator import Orchestrator
    from novelforge.control_plane.skill_registry import SkillRegistry
    from novelforge.skills import register_default_skills

    body = "平稳正文。" * 150 + CLIMAX + "尘埃落定。" * 40
    beats_json = json.dumps(PAYOFF_BEATS, ensure_ascii=False)

    def factory(messages, model="", temperature=1.0):
        user = str(messages[-1].content) if messages else ""
        if "规划任务" in user:
            return beats_json
        if "本章任务" in user:
            return f"```draft\n{body}\n```\n```proposals\n[]\n```"
        if "未解伏笔" in user:
            return '{"settlements": [], "new_hooks": []}'
        return "[]"

    fake = FakeProvider(factory=factory)
    gw = LLMGateway(fake, BudgetLedger(max_tokens=10_000_000, max_usd=100.0,
                                       max_revise_rounds=100))
    reg = SkillRegistry()
    register_default_skills(reg)
    cfg = NovelForgeConfig(project_id=project)
    cfg.provider.provider = "fake"
    cfg.recall.enable_summaries = False
    cfg.settle.enabled = False

    conn = get_registry().open_conn(project)
    try:
        _entity(conn, "e2", "玄天老祖", first=5)
        conn.commit()
        outcome = Orchestrator(gw, reg, cfg).generate_chapter(5, conn, chapter_goal="击退魔头")
    finally:
        conn.close()

    assert outcome.ok, outcome.error
    found = [i for i in outcome.issues if i.get("check") == dem.CHECK_NEWCOMER]
    assert len(found) == 1
    assert found[0]["severity"] == "warn"
    assert found[0]["source"] == "craft"
    assert found[0]["evidence"] == CLIMAX
    assert not [i for i in outcome.issues if i.get("severity") == "block"]
    revise_calls = [
        c for c in fake.calls
        if any(m in str(c["messages"][-1].content) for m in _REVISE_MARKERS)
    ]
    assert revise_calls == []


def test_governance_check_craft_endpoint(client, project):
    """/check/craft 只带 draft/beats/proposals：契约按章号从库里读，warn 照常返回。"""
    from novelforge.app.deps import get_registry

    conn = get_registry().open_conn(project)
    try:
        _entity(conn, "e2", "玄天老祖", first=5)
        conn.execute(
            "INSERT INTO chapter_cards(id, chapter, title, must_resolve_json)"
            " VALUES('c5', 5, '第五章', '[\"击退魔头\"]')"
        )
        conn.commit()
    finally:
        conn.close()
    resp = client.post(f"/v1/{project}/check/craft", json={
        "draft_text": _draft(CLIMAX), "chapter_no": 5, "beats": PLAIN_BEATS,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    [issue] = [i for i in body["craft_issues"] if i["check"] == dem.CHECK_NEWCOMER]
    assert issue["severity"] == "warn" and issue["evidence"] == CLIMAX
