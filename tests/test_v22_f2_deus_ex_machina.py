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

    def test_location_and_rule_proposals_are_not_resolvers(self, conn):
        """每卷高潮换新场地是常态：location / world_rule / style 提案的主语不是"出手者"。"""
        _volume(conn, 1, 4)
        for chapter in (1, 2, 3):
            _index_chapter(conn, chapter, "林凡下山，继续赶路。")
        climax = "危急关头，众人退入黑风谷，借灵气复苏之势，萧玄一剑击退魔头。"
        draft = _draft(climax)
        proposals = [
            {"op": "add", "fact_type": "location", "entity": "黑风谷",
             "new": {"subject": "黑风谷", "predicate": "位于", "object": "北境"}},
            {"op": "add", "fact_type": "world_rule", "entity": "灵气复苏",
             "new": {"subject": "灵气复苏", "predicate": "效果", "object": "修为暴涨"}},
            {"op": "add", "fact_type": "style", "entity": "全书",
             "new": {"subject": "全书", "predicate": "基调", "object": "热血"}},
            {"op": "add", "fact_type": "numeric", "entity": "灵石",
             "new": {"subject": "灵石", "predicate": "数量", "object": "300"}},
            # 同一批里地点也被 event 提案当主语：仍按地点处理，不报。
            {"op": "add", "fact_type": "event", "entity": "黑风谷",
             "new": {"subject": "黑风谷", "predicate": "发生", "object": "大战"}},
            {"op": "add", "fact_type": "event", "entity": "萧玄",
             "new": {"subject": "萧玄", "predicate": "击退", "object": "魔头"}},
        ]
        issues = dem.craft_checks(_ctx(conn, 4, draft, proposals=proposals))
        reported = " ".join(i["detail"] for i in issues)
        for name in ("黑风谷", "灵气复苏", "全书", "灵石"):
            assert f"「{name}」" not in reported
        # 对照：同批的真新人萧玄照报（a 已报，d 不重复）。
        [issue] = issues
        assert issue["check"] == dem.CHECK_NEWCOMER and "「萧玄」" in issue["detail"]
        assert dem._proposal_entity_names(proposals) == ["萧玄"]

    def test_short_alias_inside_longer_name_is_not_a_mention(self, conn):
        _entity(conn, "e2", "玄天老祖", first=5, aliases=["老祖"])
        _entity(conn, "e3", "血魔老祖", first=1)
        _entity(conn, "e4", "黑风", first=5)
        _entity(conn, "l1", "黑风谷", etype="location", first=1)
        draft = _draft("危急关头，血魔老祖退入黑风谷，魔头随之溃散。")
        assert dem.craft_checks(_ctx(conn, 5, draft)) == []
        # 独立出现的短别名照常命中。
        draft = _draft("危急关头，老祖从天而降，血魔老祖仓皇而逃。")
        [issue] = _checks(dem.craft_checks(_ctx(conn, 5, draft)), dem.CHECK_NEWCOMER)
        assert "「玄天老祖」" in issue["detail"]
        assert issue["span"] == "危急关头，老祖从天而降，血魔老祖仓皇而逃。"

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

    # review：本章新登场、在高潮被打脸的挑衅者曾被报成"解决者"（措辞与修复建议都反了）
    _VILLAIN_CLIMAX = ("一个黑袍青年从天而降，自称赵无极，出手便要取林凡性命。"
                       "林凡冷笑一声，一剑斩出，赵无极惨叫着倒飞出去，跪地求饶。")
    _VILLAIN_PROPOSALS = [
        {"op": "add", "fact_type": "event", "entity": "赵无极",
         "new": {"subject": "赵无极", "predicate": "挑衅", "object": "林凡"}},
    ]

    def _history(self, conn):
        _entity(conn, "e_lin", "林凡", first=1)
        for chapter in (1, 2, 3, 4):
            _index_chapter(conn, chapter, "林凡下山，继续赶路。")

    def test_new_villain_defeated_in_climax_is_not_a_resolver(self, conn):
        self._history(conn)
        draft = _draft(self._VILLAIN_CLIMAX)
        issues = dem.craft_checks(_ctx(conn, 5, draft, proposals=self._VILLAIN_PROPOSALS))
        assert _checks(issues, dem.CHECK_NEWCOMER) == []

    @pytest.mark.parametrize("climax", [
        "危急关头，林凡一剑斩杀赵无极，魔头随之溃散。",          # 打击动词的宾语
        "危急关头，赵无极被林凡一掌拍飞，再无还手之力。",        # 被动句
        "危急关头，林凡一拳轰在赵无极胸口，魔头溃散。",
    ])
    def test_new_villain_as_object_or_passive_is_not_a_resolver(self, conn, climax):
        _entity(conn, "e_zhao", "赵无极", first=5)
        _entity(conn, "e_lin", "林凡", first=1)
        assert _checks(dem.craft_checks(_ctx(conn, 5, _draft(climax))),
                       dem.CHECK_NEWCOMER) == []

    def test_new_ally_who_defeats_the_villain_is_still_a_resolver(self, conn):
        self._history(conn)
        _entity(conn, "e_xue", "血魔老祖", first=2)
        climax = "危急关头，一道剑光落下，赵无极一剑斩出，血魔老祖惨叫着倒飞出去。"
        proposals = [{"op": "add", "fact_type": "event", "entity": "赵无极",
                      "new": {"subject": "赵无极", "predicate": "击退", "object": "血魔老祖"}}]
        [issue] = _checks(dem.craft_checks(_ctx(conn, 5, _draft(climax), proposals=proposals)),
                          dem.CHECK_NEWCOMER)
        assert "「赵无极」" in issue["detail"]

    def test_new_onlooker_who_only_reacts_is_not_a_resolver(self, conn):
        _entity(conn, "e_lin", "林凡", first=1)
        _entity(conn, "e_wang", "王长老", first=5)
        climax = "林凡一剑击退魔头。王长老目瞪口呆，半晌说不出话。"
        assert _checks(dem.craft_checks(_ctx(conn, 5, _draft(climax))),
                       dem.CHECK_NEWCOMER) == []

    def test_defeated_newcomer_late_in_volume_gets_the_softer_late_wording(self, conn):
        _volume(conn, 1, 20)
        _entity(conn, "e_lin", "林凡", first=1)
        _entity(conn, "e_zhao", "赵无极", first=19)
        issues = dem.craft_checks(_ctx(conn, 19, _draft(self._VILLAIN_CLIMAX)))
        assert [i["check"] for i in issues] == [dem.CHECK_LATE_NEWCOMER]
        assert "「赵无极」" in issues[0]["detail"]

    def test_english_defeated_newcomer_is_not_a_resolver(self, conn):
        set_meta(conn, "content_language", "en-US")
        ws = {"writing_profile": {"content_language": "en-US"}}
        _entity(conn, "e_kael", "Kael", first=1)
        _entity(conn, "e_dorn", "Dorn", first=3)
        filler = "Kael crossed the bridge and counted the lanterns. " * 8
        tail = "Somewhere below, the river kept its slow count of the hours. " * 2
        for climax in ("Kael's blade flashed, and Dorn was thrown into the mud. ",
                       "Kael struck down Dorn with a single blow. ",
                       "Dorn screamed and fell to his knees before Kael. "):
            assert _checks(dem.craft_checks(_ctx(conn, 3, filler + climax + tail, workspace=ws)),
                           dem.CHECK_NEWCOMER) == [], climax
        # 对照：新人出手解决 → 照报
        rescue = "Then Dorn stepped out of the fog and broke the siege. "
        assert len(_checks(dem.craft_checks(_ctx(conn, 3, filler + rescue + tail, workspace=ws)),
                           dem.CHECK_NEWCOMER)) == 1


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

    def test_negated_use_in_climax_is_silent(self, conn):
        _gimmick(conn, "g1", "血遁术", 5)
        for climax in ("他想起血遁术的传说，却终究没敢动用。", "林凡没有动用血遁术，咬牙硬扛。",
                       "林凡不敢施展血遁术，转身迎敌。"):
            assert _checks(dem.craft_checks(_ctx(conn, 5, _draft(climax))),
                           dem.CHECK_UNDEMONSTRATED) == [], climax

    def test_mention_only_in_climax_uses_softer_wording(self, conn):
        _gimmick(conn, "g1", "血遁术", 5)
        climax = "魔头狞笑：血遁术也救不了你！"
        draft = _draft(climax)
        [issue] = _checks(dem.craft_checks(_ctx(conn, 5, draft)), dem.CHECK_UNDEMONSTRATED)
        _assert_shape(issue, draft)
        assert issue["detail"].startswith("能力未经铺垫就出现在高潮段：「血遁术」本章才首次设定")
        assert "被用来解决问题" not in issue["detail"]
        assert issue["span"] == climax

    def test_use_proposal_evidence_in_climax_counts_as_use(self, conn):
        _gimmick(conn, "g1", "血遁术", 5)
        proposals = [{"op": "add", "fact_type": "power_system", "entity": "林凡",
                      "new": {"facet": "gimmick_use", "gimmick_name": "血遁术"},
                      "evidence_refs": [{"quote": "一道血光遁出千里"}]}]
        draft = _draft("林凡掐诀，一道血光遁出千里。")
        [issue] = _checks(dem.craft_checks(_ctx(conn, 5, draft, proposals=proposals)),
                          dem.CHECK_UNDEMONSTRATED)
        assert "被用来解决问题" in issue["detail"]
        assert issue["span"] == "林凡掐诀，一道血光遁出千里。"

    def _canon_fact(self, conn, fid, subject, obj, chapter, detail):
        conn.execute(
            "INSERT INTO facts(id, fact_type, subject, predicate, object, detail_json, status,"
            " valid_from_chapter, current_revision_id) VALUES(?,?,?,?,?,?,'canon',?,?)",
            (fid, "power_system", subject, "金手指", obj,
             json.dumps(detail, ensure_ascii=False), chapter, f"r_{fid}"),
        )

    def test_defining_rule_fact_alone_is_not_a_demonstration(self, conn):
        # 定义 fact 由 gimmick_rules.fact_id 指向（detail 不带 facet）；另一条 facet=gimmick_rule
        # 的旧定义也不算展示。两条都排除后仍应报"此前从未展示或使用过"。
        self._canon_fact(conn, "f_def", "林凡", "血遁术", 2, {"gimmick_name": "血遁术"})
        self._canon_fact(conn, "f_old", "林凡", "血遁术", 2,
                         {"facet": "gimmick_rule", "gimmick_name": "血遁术"})
        conn.execute(
            "INSERT INTO gimmick_rules(id, gimmick_name, valid_from_chapter, fact_id,"
            " source_fact_id) VALUES('g1', '血遁术', 2, 'f_def', 'f_def')"
        )
        _index_chapter(conn, 1, "林凡下山。")
        draft = _draft("林凡施展血遁术，逃出生天。")
        [issue] = _checks(dem.craft_checks(_ctx(conn, 5, draft)), dem.CHECK_UNDEMONSTRATED)
        assert "此前从未展示或使用过" in issue["detail"]

    def test_earlier_canon_usage_fact_counts_as_demonstrated(self, conn):
        _gimmick(conn, "g1", "血遁术", 2)
        _index_chapter(conn, 1, "林凡下山。")
        draft = _draft("林凡施展血遁术，逃出生天。")
        assert _checks(dem.craft_checks(_ctx(conn, 5, draft)), dem.CHECK_UNDEMONSTRATED)
        for fid, subject, obj, detail in (
            ("f_use", "林凡", "脱身", {"facet": "gimmick_use", "gimmick_name": "血遁术"}),
            ("f_obj", "林凡", "血遁术", {}),
        ):
            conn.execute("DELETE FROM facts")
            self._canon_fact(conn, fid, subject, obj, 3, detail)
            assert _checks(dem.craft_checks(_ctx(conn, 5, draft)),
                           dem.CHECK_UNDEMONSTRATED) == [], fid
        # 本章（N）及以后的 canon fact 不算"此前"。
        conn.execute("DELETE FROM facts")
        self._canon_fact(conn, "f_now", "林凡", "血遁术", 5, {"facet": "gimmick_use"})
        assert _checks(dem.craft_checks(_ctx(conn, 5, draft)), dem.CHECK_UNDEMONSTRATED)

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

    def test_double_negation_is_a_use(self, conn):
        _gimmick(conn, "g1", "血遁术", 0, cost={"寿元": 10})
        for draft in ("林凡不得不施展血遁术，逃出生天。", "林凡忍不住催动血遁术，逃出生天。"):
            [issue] = dem.craft_checks(_ctx(conn, 1, draft, beats=PLAIN_BEATS))
            assert issue["check"] == dem.CHECK_UNPAID_COST, draft
        assert dem._sentence_uses("不得不施展血遁术", 5, 8, False)
        assert not dem._sentence_uses("不敢施展血遁术", 4, 7, False)

    def test_cost_without_searchable_keyword_is_silent(self, conn):
        _gimmick(conn, "g1", "血遁术", 0, cost={"amount": 3})
        assert dem.craft_checks(_ctx(conn, 1, "林凡施展血遁术。", beats=PLAIN_BEATS)) == []

    def test_cost_keyword_extraction(self):
        assert dem._cost_keywords({"寿元": 10}) == ["寿元"]
        assert dem._cost_keywords("灵力耗尽") == ["灵力耗尽", "灵力"]
        assert dem._cost_keywords({"type": "精血", "amount": "三成"}) == ["精血"]
        assert dem._cost_keywords(["a memory", "ten years of lifespan"]) == ["memory", "lifespan"]

    @pytest.mark.parametrize("cost,core", [
        # review：只剥固定动词前缀/结果后缀，名词核心抽不出来 → 换个说法兑现也报"代价未付"
        ("精神力消耗", "精神力"), ("短暂虚弱", "虚弱"), ("折寿十年", "寿"),
        ("每次使用后虚弱三日", "虚弱"), ("内力消耗", "内力"),
    ])
    def test_cost_keyword_noun_core(self, cost, core):
        assert core in dem._cost_keywords(cost)

    def test_quantity_lead_in_does_not_leave_garbage(self):
        assert dem._cost_keywords("三日内无法动用灵力") == ["灵力"]

    @pytest.mark.parametrize("cost,paid", [
        ("精神力消耗", "他只觉精神力大量消耗，头痛欲裂。"),
        ("折寿十年", "他鬓角霜白，生生折了十年寿。"),
        ("短暂虚弱", "落地后他虚弱得几乎站不稳。"),
        ("三日内无法动用灵力", "此后三日，他体内灵力枯竭，连火球都搓不出来。"),
    ])
    def test_paraphrased_cost_in_prose_counts_as_paid(self, conn, cost, paid):
        _gimmick(conn, "g1", "血遁术", 0, cost=cost)
        draft = "林凡催动血遁术，遁出百里。" + paid
        assert dem.craft_checks(_ctx(conn, 1, draft, beats=PLAIN_BEATS)) == []
        # 对照：正文没写代价 → 照报
        [issue] = dem.craft_checks(_ctx(conn, 1, "林凡催动血遁术，遁出百里。", beats=PLAIN_BEATS))
        assert issue["check"] == dem.CHECK_UNPAID_COST

    @pytest.mark.parametrize("draft", [
        "此刻他已无法再施展血遁术。", "他从不轻易使用血遁术。", "林凡不再动用血遁术。",
    ])
    def test_negator_with_adverb_is_not_a_use(self, conn, draft):
        _gimmick(conn, "g1", "血遁术", 0, cost={"寿元": 10})
        assert dem.craft_checks(_ctx(conn, 1, draft, beats=PLAIN_BEATS)) == []
        assert not dem._sentence_uses(draft, draft.index("血遁术"),
                                      draft.index("血遁术") + 3, False)

    def test_double_negation_with_adverb_is_still_a_use(self):
        draft = "林凡不得不再次施展血遁术。"
        assert dem._sentence_uses(draft, draft.index("血遁术"), draft.index("血遁术") + 3, False)


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

    def test_volume_finale_hook_character_is_not_flagged(self, conn):
        # review：卷末章节以新人物现身作钩子是连载常态，不应按"卷末不宜引入新人"报
        _volume(conn, 1, 20)
        _entity(conn, "e9", "萧瑾", first=19)
        hook = "山顶上，一道黑影缓缓现身，正是魔宗少主萧瑾。"
        draft = FILLER * 8 + hook
        assert dem.craft_checks(_ctx(conn, 19, draft, beats=PLAIN_BEATS)) == []
        # 同一人物在正文中段登场 → 照报
        draft = FILLER * 4 + hook + FILLER * 4
        [issue] = dem.craft_checks(_ctx(conn, 19, draft, beats=PLAIN_BEATS))
        assert issue["check"] == dem.CHECK_LATE_NEWCOMER and "「萧瑾」" in issue["detail"]

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

    def test_english_mention_only_wording(self, conn):
        ws = self._en(conn)
        _gimmick(conn, "g1", "Starfall", 5)
        filler = "The harbor bells rang through the cold morning. " * 6
        draft = filler + "The sky split as Starfall answered the call. " + filler[:120]
        [issue] = _checks(dem.craft_checks(_ctx(conn, 5, draft, workspace=ws)),
                          dem.CHECK_UNDEMONSTRATED)
        assert issue["detail"].startswith(
            "Ability appears in the climax without setup: Starfall is first established"
        )
        assert not _CJK.search(issue["detail"] + issue["fix"])

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

    def test_bigram_fallback_index_cannot_prove_absence(self, conn, monkeypatch):
        """回退分词的 FTS 去空格后搜不到 3 字以上的名字：判"无法判断"，不判"新"。"""
        import novelforge.tokenizer as tokenizer_mod
        _entity(conn, "e2", "玄天老祖")
        _entity(conn, "e3", "萧玄")
        _index_chapter(conn, 1, "林凡下山。")
        _index_chapter(conn, 2, "林凡继续赶路。")
        draft = _draft("危急关头，玄天老祖与萧玄从天而降，一掌击退魔头。")

        def reported():
            issues = _checks(dem.craft_checks(_ctx(conn, 3, draft)), dem.CHECK_NEWCOMER)
            return sorted(re.findall(r"「(.+?)」", " ".join(i["detail"] for i in issues)))

        assert reported() == ["玄天老祖", "萧玄"]
        monkeypatch.setattr(tokenizer_mod, "_HAS_JIEBA", False)
        assert reported() == ["萧玄"]            # 2 字名仍可由 bigram 判定
        monkeypatch.setattr(tokenizer_mod, "_HAS_JIEBA", True)
        set_meta(conn, "tokenizer_version", "zh-CN:fallback-bigram-1+dict:000000000000")
        assert reported() == ["萧玄"]

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


def _generate_chapter_5(project, configure=None):
    """FakeProvider 跑一遍 generate_chapter(5)；configure(cfg) 可改 NovelForgeConfig。"""
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
    if configure is not None:
        configure(cfg)

    conn = get_registry().open_conn(project)
    try:
        _entity(conn, "e2", "玄天老祖", first=5)
        conn.commit()
        outcome = Orchestrator(gw, reg, cfg).generate_chapter(5, conn, chapter_goal="击退魔头")
    finally:
        conn.close()
    assert outcome.ok, outcome.error
    return outcome, fake


def test_generate_chapter_reports_deus_ex_machina_as_warn(project):
    outcome, fake = _generate_chapter_5(project)
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


def test_generate_chapter_honours_plausibility_config(project):
    """orchestrator 写入的 workspace["cfg"] 把 cfg.plausibility 送到检查里。"""
    def disable(cfg):
        cfg.plausibility.enabled = False

    outcome, _ = _generate_chapter_5(project, disable)
    assert not [i for i in outcome.issues
                if str(i.get("category", "")).startswith("craft.deus_ex_machina.")]


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
