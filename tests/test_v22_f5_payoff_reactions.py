"""v22/F5 爽点三段式：在场者反应检测、actuals 回填、craft 告警、信息差提示、结算报告。

全部确定性、无网络（FakeProvider / 本地 SQLite）。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from novelforge.config import NovelForgeConfig, PayoffConfig
from novelforge.control_plane.skill_base import SkillContext
from novelforge.craft import payoff_reactions as pr
from novelforge.craft.payoff_binding import check_payoff_binding
from novelforge.db.connection import init_db, init_db_from_conn, set_meta
from novelforge.ids import new_id
from novelforge.planning.actuals import extract_chapter_actuals
from novelforge.planning.context import PlanContext
from novelforge.planning.models import ChapterActuals

# 与 FakeProvider 路由冲突的标记（新提示块里一个都不能出现）
_ROUTING_MARKERS = (
    "规划任务", "## Planning Task", "本章任务", "## Chapter Task", "草稿：", "### 候选",
    "本章目标：", "Chapter goal: ", "一致性问题", "修订补丁任务", "润色补丁任务", "工艺问题",
    "未解伏笔", "章正文：", "各章摘要", "卷《", "章正文如下：", "场景原文",
)

_PAYOFF_BEATS = [
    {"seq": 1, "beat_type": "setup", "summary": "演武场比试开始", "value_axis": "平静→紧张"},
    {"seq": 2, "beat_type": "payoff_beat", "summary": "林凡一招击败赵师兄",
     "value_axis": "轻视→敬畏", "witnesses": ["王长老", "赵师兄"]},
    {"seq": 3, "beat_type": "hook", "summary": "黑影一闪", "value_axis": "悬念↑"},
]


# ── fixtures / helpers ───────────────────────────────────────────────────────

@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db_from_conn(c)
    yield c
    c.close()


def _entity(conn, name: str, etype: str = "character") -> str:
    eid = new_id("ent")
    conn.execute(
        "INSERT INTO entities(id, canonical_name, entity_type) VALUES(?,?,?)", (eid, name, etype),
    )
    return eid


def _knowledge(conn, knower: str, secret: str, state: str, chapter: int, **extra) -> None:
    conn.execute(
        "INSERT INTO knowledge_edges(id, knower_entity_id, secret_key, knowledge_state,"
        " learned_chapter, public_from_chapter, secrecy_level) VALUES(?,?,?,?,?,?,?)",
        (new_id("ke"), knower, secret, state, chapter,
         extra.get("public_from_chapter"), extra.get("secrecy_level")),
    )


def _cast(conn) -> dict[str, str]:
    """林凡（主角，经 meta_kv 自由文本解析）+ 王长老 + 赵师兄。"""
    ids = {name: _entity(conn, name) for name in ("林凡", "王长老", "赵师兄")}
    set_meta(conn, "protagonist", "林凡：被逐出宗门的少年，身负血仇")
    conn.commit()
    return ids


def _ctx(conn, chapter: int, draft: str, beats=None, **workspace) -> SkillContext:
    ws = {"draft_text": draft, "beats": beats if beats is not None else _PAYOFF_BEATS}
    ws.update(workspace)
    return SkillContext(
        project_id="p", target_chapter=chapter, mode="human_gate", as_of_chapter=chapter - 1,
        budget=None, llm=None, conn=conn, workspace=ws,
    )


def _spans_ok(draft: str, reactions) -> bool:
    return all(
        draft[s.start_char:s.end_char] == s.text for r in reactions for s in r.evidence_spans
    )


def _store_contract(conn, chapter: int, beats: list[dict]) -> None:
    conn.execute(
        "INSERT INTO chapter_cards(id, chapter, goal, status, locked) VALUES(?,?,?,?,1)",
        (new_id("card"), chapter, "比试", "drafted"),
    )
    for beat in beats:
        conn.execute(
            "INSERT INTO beats(id, chapter, seq, beat_type, summary, value_axis, witnesses_json)"
            " VALUES(?,?,?,?,?,?,?)",
            (new_id("beat"), chapter, beat["seq"], beat["beat_type"], beat["summary"],
             beat.get("value_axis"), json.dumps(beat.get("witnesses") or [], ensure_ascii=False)),
        )


def _store_actuals(conn, chapter: int, reactions: list[dict]) -> None:
    """A new draft revision whose actuals become the chapter's current row."""
    draft_id = new_id("draft")
    revision = conn.execute(
        "SELECT COUNT(*) FROM draft_index WHERE chapter=?", (chapter,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO draft_index(id,chapter,revision_round,file_path,sha256,status)"
        " VALUES(?,?,?,?,?,'draft')", (draft_id, chapter, revision, f"l0/{draft_id}.txt", draft_id),
    )
    conn.execute(
        "UPDATE chapter_actuals SET status='superseded', superseded_at=datetime('now')"
        " WHERE chapter=? AND status='current'", (chapter,),
    )
    payload = ChapterActuals(chapter=chapter, draft_id=draft_id, reactions=reactions)
    conn.execute(
        "INSERT INTO chapter_actuals(id,chapter,draft_id,payload_json,extractor_version,status)"
        " VALUES(?,?,?,?,?,'current')",
        (new_id("actual"), chapter, draft_id,
         json.dumps(payload.model_dump(mode="json"), ensure_ascii=False), "deterministic-v1"),
    )
    conn.commit()


def _commit_chapter(db, db_path, chapter: int, text: str, beats: list[dict]) -> None:
    """真实管线函数：L0 落盘（_persist_draft）+ actuals/对账落库（evaluate_and_persist_alignment）。"""
    from novelforge.control_plane.orchestrator import _persist_draft
    from novelforge.planning.alignment import evaluate_and_persist_alignment

    draft_id = _persist_draft(db, text, chapter, "f5", str(db_path))
    assert draft_id
    evaluate_and_persist_alignment(
        db, _plan_context(chapter, beats=beats), text, chapter, draft_id=draft_id,
        seed_proposals=[],
    )
    db.commit()


def _current_reactions(db, chapter: int) -> list[dict]:
    row = db.execute(
        "SELECT payload_json FROM chapter_actuals WHERE chapter=? AND status='current'", (chapter,),
    ).fetchone()
    return json.loads(row[0])["reactions"]


# ── 1. 纯函数检测器 ───────────────────────────────────────────────────────────

class TestDetector:
    def test_zh_witness_and_crowd_with_exact_offsets(self):
        draft = "林凡一掌拍出。\n王长老脸色大变，众人倒吸一口凉气。赵师兄面如死灰。"
        found = pr.detect_reactions(draft, ["王长老", "赵师兄"], language="zh-CN",
                                    protagonist_names=["林凡"], beat_seq=2, target="击败赵师兄")
        assert [(r.reactor, r.reaction) for r in found] == [
            ("王长老", "脸色大变"), ("众人", "倒吸一口凉气"), ("赵师兄", "面如死灰"),
        ]
        assert _spans_ok(draft, found)
        assert found[0].evidence_spans[0].text == "王长老脸色大变，众人倒吸一口凉气。"
        assert {r.beat_seq for r in found} == {2}
        assert found[0].target == "击败赵师兄"
        assert found[0].attitude_shift == "fear" and found[1].attitude_shift == "shock"

    def test_protagonist_is_never_a_reactor(self):
        assert pr.detect_reactions("林凡震惊地看着王长老。", ["王长老"], language="zh-CN",
                                   protagonist_names=["林凡"]) == []
        assert pr.detect_reactions("林凡打败了赵师兄，震惊不已。", ["赵师兄"],
                                   language="zh-CN", protagonist_names=["林凡"]) == []
        # 规划把主角误列为 witness：照样不算反应
        assert pr.detect_reactions("林凡愣住了。", ["林凡"], language="zh-CN",
                                   protagonist_names=["林凡"]) == []
        # 主角作主语、同分句里的非主角才是反应者
        found = pr.detect_reactions("林凡看着王长老脸色大变。", ["王长老"], language="zh-CN",
                                    protagonist_names=["林凡"])
        assert [r.reactor for r in found] == ["王长老"]

    @pytest.mark.parametrize("draft,expected", [
        ("王长老看着林凡，脸色大变。", ["王长老"]),             # 承前省略主语
        ("脸色大变的王长老后退一步。", ["王长老"]),             # 定语式
        ("王长老、赵师兄目瞪口呆。", ["王长老", "赵师兄"]),     # 并列主语
        ("王长老盯着碎玉。他脸色大变。", ["王长老"]),           # 代词承接上一句
        ("赵师兄还想再说。林凡收剑而立。对方面如死灰。", ["赵师兄"]),  # 对手指代
        ("林凡盯着碎玉。他脸色大变。", []),                    # 代词指向主角
        ("赵师兄终于被林凡制服了。", []),                      # "制服了"不是"服了"
        ("赵师兄低声道：“我服了。”", []),                      # 引号内的话不算反应
        ("林凡向王长老道歉。", []),                            # 主角朝向宾语的动作不归宾语
        ("林凡朝赵师兄咬牙道：“再来！”", []),
    ])
    def test_zh_attribution(self, draft, expected):
        found = pr.detect_reactions(draft, ["王长老", "赵师兄"], language="zh-CN",
                                    protagonist_names=["林凡"])
        assert [r.reactor for r in found] == expected
        assert _spans_ok(draft, found)

    def test_quoted_speech_is_not_a_reaction(self):
        draft = "林凡冷笑道：“王长老，你后悔了吗？”"
        assert pr.detect_reactions(draft, ["王长老"], language="zh-CN",
                                   protagonist_names=["林凡"]) == []
        draft = "“这不可能！”王长老惊呼道。"
        found = pr.detect_reactions(draft, ["王长老"], language="zh-CN")
        assert [(r.reactor, r.reaction) for r in found] == [("王长老", "惊呼")]
        assert found[0].evidence_spans[0].text == "王长老惊呼道。"

    def test_english_detection(self):
        draft = ("Lin Fan's palm landed. Brother Zhao flew backward, and the crowd gasped.\n"
                 "Elder Wang's face went pale. Lin Fan stared at Elder Wang.\n"
                 "\"You regret it now?\" Lin Fan asked. Wang knelt.")
        found = pr.detect_reactions(draft, ["Elder Wang", "Brother Zhao"], language="en-US",
                                    protagonist_names=["Lin Fan"])
        assert [(r.reactor, r.reaction) for r in found] == [
            ("the crowd", "gasped"), ("Elder Wang", "went pale"), ("Elder Wang", "knelt"),
        ]
        assert _spans_ok(draft, found)
        assert all(r.reactor != "Lin Fan" for r in found)

    def test_language_inferred_and_empty_inputs(self):
        assert pr.infer_language("王长老脸色大变。") == "zh-CN"
        assert pr.infer_language("The crowd gasped.") == "en-US"
        assert [r.reactor for r in pr.detect_reactions("The hall fell silent.", [])] == ["the hall"]
        assert pr.detect_reactions("", ["王长老"]) == []
        assert pr.detect_reactions("王长老点了点头。", ["王长老"], language="zh-CN") == []

    def test_per_reactor_cap_and_aliases(self):
        draft = "王老怪愣住了。" * 5
        found = pr.detect_reactions(draft, ["王长老"], language="zh-CN",
                                    aliases={"王长老": ["王老怪"]})
        assert [r.reactor for r in found] == ["王长老"] * 3

    @pytest.mark.parametrize("draft", [
        "赵师兄抱拳道：“林师弟，请。”",          # 礼节不是服软
        "王长老跪坐在蒲团上讲道。",              # 姿态不是下跪
        "赵师兄后退一步，摆出架势。",            # 身法不是受惊后退
        "王长老躬身行礼，转身离去。",
    ])
    def test_zh_courtesy_and_stage_directions_are_not_reactions(self, draft):
        assert pr.detect_reactions(draft, ["王长老", "赵师兄"], language="zh-CN",
                                   protagonist_names=["林凡"]) == []

    @pytest.mark.parametrize("draft,expected", [
        ("赵师兄抱拳认输。", ("赵师兄", "抱拳认输")),
        ("王长老扑通一声跪了下去。", ("王长老", "跪了")),
        ("赵师兄被震得连退三步。", ("赵师兄", "连退")),
    ])
    def test_zh_qualified_submission_and_fear_cues(self, draft, expected):
        found = pr.detect_reactions(draft, ["王长老", "赵师兄"], language="zh-CN",
                                    protagonist_names=["林凡"])
        assert [(r.reactor, r.reaction) for r in found] == [expected]

    @pytest.mark.parametrize("draft", [
        "Elder Wang stared at the scroll for a long time.",
        "Zhao bowed politely and left.",
        "Everyone erupted in laughter at the joke.",
        "Elder Wang stepped back to make room.",
        "They spoke in hushed voices; Elder Wang nodded.",
    ])
    def test_en_courtesy_and_stage_directions_are_not_reactions(self, draft):
        assert pr.detect_reactions(draft, ["Elder Wang", "Brother Zhao"], language="en-US",
                                   protagonist_names=["Lin Fan"]) == []

    @pytest.mark.parametrize("draft,expected", [
        ("Brother Zhao bowed his head.", ("Brother Zhao", "bowed his head")),
        ("The hall erupted in cheers.", ("the hall", "erupted in cheers")),
        ("Elder Wang stared in disbelief.", ("Elder Wang", "stared in disbelief")),
        ("Elder Wang stumbled backward.", ("Elder Wang", "stumbled backward")),
    ])
    def test_en_qualified_cues(self, draft, expected):
        found = pr.detect_reactions(draft, ["Elder Wang", "Brother Zhao"], language="en-US",
                                    protagonist_names=["Lin Fan"])
        assert [(r.reactor, r.reaction) for r in found] == [expected]

    # review：词表漏掉大量网文常用反应句式 → 误报"爽点无反应"
    _ZH_STAPLES = (
        "脸色变了", "脸色顿时变了", "的脸色变得极为难看", "脸色一僵", "面色一白",
        "脸色青一阵白一阵", "脸色阴晴不定", "瞪大了眼睛", "双眼圆睁", "眼珠子都快瞪出来了",
        "心中一凛", "心头一凛", "心中大骇", "大骇", "暗自心惊", "倒抽一口冷气", "倒抽了口凉气",
        "倒吸一口气", "如遭雷击", "面面相觑", "议论纷纷", "僵在原地", "笑容僵在脸上",
        "笑容凝固", "嘴角抽搐", "哑然", "语塞", "半晌无言", "沉默了", "傻了", "懵了",
        "人都傻了", "两股战战", "瘫软在地", "浑身冰凉", "额头冒出冷汗", "眼中闪过一丝忌惮",
        "握剑的手微微发抖", "吓了一跳", "震撼不已", "失语",
    )
    _EN_STAPLES = (
        "'s face went slack", "'s jaw tightened", "'s eyes narrowed", " stiffened",
        " went rigid", "'s breath caught", "'s grin vanished", "'s smile faded",
        " felt the color drain from his face", " swallowed nervously",
        " took an involuntary step back", " sputtered", " stammered", " went still",
    )

    @pytest.mark.parametrize("cue", _ZH_STAPLES)
    def test_zh_staple_reactions_are_recalled(self, cue):
        found = pr.detect_reactions(f"王虎{cue}。", ["王虎"], language="zh-CN",
                                    protagonist_names=["林凡"])
        assert [r.reactor for r in found] == ["王虎"]

    @pytest.mark.parametrize("cue", _EN_STAPLES)
    def test_en_staple_reactions_are_recalled(self, cue):
        found = pr.detect_reactions(f"Dorn{cue}.", ["Dorn"], language="en-US",
                                    protagonist_names=["Kael"])
        assert [r.reactor for r in found] == ["Dorn"]

    @pytest.mark.parametrize("draft,expected", [
        ("A murmur ran through the crowd.", ("the crowd", "A murmur ran")),
        ("Gasps rippled through the hall.", ("the hall", "Gasps")),
        ("Whispers broke out among the onlookers.", ("the onlookers", "Whispers broke out")),
        ("A hush fell over the room.", ("the room", "A hush fell")),
        ("The crowd murmured.", ("the crowd", "murmured")),
    ])
    def test_en_crowd_after_cue_is_credited(self, draft, expected):
        found = pr.detect_reactions(draft, [], language="en-US", protagonist_names=["Kael"])
        assert [(r.reactor, r.reaction) for r in found] == [expected]

    def test_zh_crowd_after_cue_is_credited(self):
        found = pr.detect_reactions("惊呼声响彻全场。", [], language="zh-CN",
                                    protagonist_names=["林凡"])
        assert [(r.reactor, r.reaction) for r in found] == [("全场", "惊呼")]

    def test_bare_murmured_speech_is_not_a_reaction(self):
        assert pr.detect_reactions("Elder Wang murmured a reply and left.", ["Elder Wang"],
                                   language="en-US", protagonist_names=["Lin Fan"]) == []

    @pytest.mark.parametrize("draft,language", [
        ("王虎脸上没有丝毫震惊，反而笑了起来。", "zh-CN"),
        ("王虎毫不畏惧。", "zh-CN"),
        ("王虎并未愣住。", "zh-CN"),
        ("王虎并不后悔。", "zh-CN"),
        ("Wang Hu did not flinch.", "en-US"),
        ("Wang Hu never gasped.", "en-US"),
        ("Wang Hu didn't even flinch.", "en-US"),
    ])
    def test_negated_reactions_are_not_credited(self, draft, language):
        assert pr.detect_reactions(draft, ["王虎", "Wang Hu"], language=language,
                                   protagonist_names=["林凡", "Kael"]) == []

    @pytest.mark.parametrize("draft,language,reaction", [
        ("王虎从未如此震惊过。", "zh-CN", "震惊"),
        ("王虎忍不住倒吸一口凉气。", "zh-CN", "倒吸一口凉气"),
        ("王虎不由得愣住了。", "zh-CN", "愣住"),
        ("Wang Hu couldn't help but gasp.", "en-US", "gasp"),
    ])
    def test_non_negating_lead_ins_still_count(self, draft, language, reaction):
        found = pr.detect_reactions(draft, ["王虎", "Wang Hu"], language=language,
                                    protagonist_names=["林凡", "Kael"])
        assert [r.reaction for r in found] == [reaction]

    @pytest.mark.parametrize("draft,reactor,reaction", [
        # 双重否定 = 强调肯定：否定过滤不能把群像反应吞掉（integrated recheck 回归）
        ("在场众人无不震惊。", "在场众人", "震惊"),
        ("众人无不骇然。", "众人", "骇然"),
        ("看得众人无不倒吸一口凉气。", "众人", "倒吸一口凉气"),
        ("众人莫不脸色大变。", "众人", "脸色大变"),
        ("众人无一不目瞪口呆。", "众人", "目瞪口呆"),
        ("王虎不得不低下头。", "王虎", "低下头"),
    ])
    def test_double_negation_is_affirmative(self, draft, reactor, reaction):
        found = pr.detect_reactions(draft, ["王虎"], language="zh-CN",
                                    protagonist_names=["林凡"])
        assert [(r.reactor, r.reaction) for r in found] == [(reactor, reaction)]


# ── 2. enrich_actuals → chapter_actuals 持久化 ────────────────────────────────

def _plan_context(chapter: int = 5, beats=None) -> PlanContext:
    return PlanContext(target_chapter=chapter, current_contract={
        "chapter": chapter, "goal": "演武场比试", "beats": beats or _PAYOFF_BEATS,
    })


class TestEnrichActuals:
    DRAFT = ("演武场比试开始。林凡一招击败赵师兄。\n"
             "王长老脸色大变。赵师兄面如死灰，众人哗然。黑影一闪。")

    def test_reactions_bound_to_payoff_beat_and_deduped(self):
        extraction = extract_chapter_actuals(5, self.DRAFT, _plan_context())
        reactions = extraction.actuals.reactions
        assert [r.reactor for r in reactions] == ["王长老", "赵师兄", "众人"]
        assert {r.beat_seq for r in reactions} == {2}
        assert reactions[0].target == "林凡一招击败赵师兄"
        assert _spans_ok(self.DRAFT, reactions)
        # 再跑一次：同 reactor + 区间不重复追加
        pr.enrich_actuals(extraction.actuals, self.DRAFT, _plan_context())
        assert len(extraction.actuals.reactions) == 3

    def test_no_payoff_beat_or_empty_draft_leaves_reactions_empty(self):
        beats = [b for b in _PAYOFF_BEATS if b["beat_type"] != "payoff_beat"]
        assert extract_chapter_actuals(5, self.DRAFT, _plan_context(beats=beats)).actuals.reactions == []
        actuals = ChapterActuals(chapter=5)
        pr.enrich_actuals(actuals, "", _plan_context())
        pr.enrich_actuals(actuals, self.DRAFT, None)
        assert actuals.reactions == []

    def test_multiple_payoff_beats_bind_by_witness_and_position(self):
        beats = [
            {"seq": 1, "beat_type": "payoff_beat", "summary": "林凡击败赵师兄",
             "witnesses": ["赵师兄"]},
            {"seq": 2, "beat_type": "payoff_beat", "summary": "林凡亮出宗主令牌",
             "witnesses": ["王长老"]},
            {"seq": 3, "beat_type": "hook", "summary": "黑影"},
        ]
        draft = ("林凡击败赵师兄，赵师兄面如死灰。众人哗然。"
                 "林凡亮出宗主令牌。王长老跪了下去，全场死寂，众人倒吸一口凉气。")
        reactions = extract_chapter_actuals(5, draft, _plan_context(beats=beats)).actuals.reactions
        by_reactor = {(r.reactor, r.beat_seq) for r in reactions}
        assert ("赵师兄", 1) in by_reactor and ("王长老", 2) in by_reactor
        assert ("众人", 1) in by_reactor            # 第一次哗然在第 1 拍之后
        crowd_late = [r for r in reactions if r.reactor == "众人"][-1]
        assert crowd_late.beat_seq == 2             # 倒吸凉气在第 2 拍之后

    def test_reactions_persist_through_evaluate_and_persist_alignment(self, tmp_path):
        from novelforge.planning.alignment import evaluate_and_persist_alignment

        db = init_db(tmp_path / "align.db")
        db.execute(
            "INSERT INTO draft_index(id,chapter,revision_round,file_path,sha256,status)"
            " VALUES('draft-5',5,0,'l0/a.txt','sha','draft')"
        )
        evaluation = evaluate_and_persist_alignment(
            db, _plan_context(), self.DRAFT, 5, draft_id="draft-5", seed_proposals=[],
        )
        db.commit()
        assert evaluation.actuals_id
        payload = json.loads(db.execute(
            "SELECT payload_json FROM chapter_actuals WHERE chapter=5 AND status='current'"
        ).fetchone()["payload_json"])
        assert [r["reactor"] for r in payload["reactions"]] == ["王长老", "赵师兄", "众人"]
        span = payload["reactions"][0]["evidence_spans"][0]
        assert self.DRAFT[span["start_char"]:span["end_char"]] == span["text"]
        db.close()


# ── 3. craft_checks ───────────────────────────────────────────────────────────

class TestCraftChecks:
    def test_reaction_present_is_silent(self, conn):
        _cast(conn)
        draft = "林凡一招击败赵师兄。王长老脸色大变，赵师兄面如死灰。"
        assert pr.craft_checks(_ctx(conn, 3, draft)) == []

    def test_no_reaction_warns_and_names_witnesses(self, conn):
        _cast(conn)
        draft = "演武场比试开始。林凡一招击败赵师兄。林凡转身离开。"
        issues = pr.craft_checks(_ctx(conn, 3, draft))
        assert len(issues) == 1
        issue = issues[0]
        assert issue["severity"] == "warn" and issue["check"] == "payoff_reaction"
        assert issue["category"] == "craft.payoff_no_reaction"
        assert issue["detail"].startswith("爽点无反应：")
        assert "（在场者 王长老/赵师兄 没有可见反应）" in issue["detail"]
        assert "王长老、赵师兄" in issue["fix"] and "下一章" in issue["fix"]
        assert issue["span"] and issue["span"] in draft

    def test_protagonist_reacting_does_not_count(self, conn):
        _cast(conn)
        issues = pr.craft_checks(_ctx(conn, 3, "林凡一招击败赵师兄，林凡震惊地看着王长老。"))
        assert [i["category"] for i in issues] == ["craft.payoff_no_reaction"]

    def test_crowd_only_is_a_softer_warning(self, conn):
        _cast(conn)
        issues = pr.craft_checks(_ctx(conn, 3, "林凡一招击败赵师兄。众人倒吸一口凉气。"))
        assert [i["category"] for i in issues] == ["craft.payoff_crowd_only"]
        assert "众人" in issues[0]["detail"] and "王长老/赵师兄" in issues[0]["detail"]
        assert issues[0]["span"] == "众人倒吸一口凉气。"

    def test_alias_counts_as_witness(self, conn):
        ids = _cast(conn)
        conn.execute("INSERT INTO entity_aliases(id, entity_id, alias) VALUES('a1', ?, '王老怪')",
                     (ids["王长老"],))
        conn.commit()
        assert pr.craft_checks(_ctx(conn, 3, "林凡一招击败赵师兄。王老怪愣住了。")) == []

    def test_english_project_gets_english_messages(self, conn):
        set_meta(conn, "content_language", "en-US")
        beats = [{"seq": 1, "beat_type": "payoff_beat", "summary": "Lin Fan defeats Zhao",
                  "witnesses": ["Elder Wang"]},
                 {"seq": 2, "beat_type": "hook", "summary": "A shadow"}]
        issues = pr.craft_checks(_ctx(conn, 3, "Lin Fan won the duel and walked away.", beats))
        assert len(issues) == 1
        assert issues[0]["detail"].startswith("Payoff without a reaction:")
        assert "Elder Wang" in issues[0]["detail"]
        assert not pr._CJK_RE.search(issues[0]["detail"] + issues[0]["fix"])

    def test_silent_without_payoff_beat_and_tolerates_bare_contexts(self, conn):
        beats = [b for b in _PAYOFF_BEATS if b["beat_type"] != "payoff_beat"]
        assert pr.craft_checks(_ctx(conn, 3, "平稳正文。", beats)) == []
        assert pr.craft_checks(object()) == []
        # governance /check/craft 形态：只有 draft/beats、无 conn、无 llm
        bare = SkillContext(project_id="p", target_chapter=1, mode="human_gate",
                            as_of_chapter=0, budget=None, llm=None, conn=None,
                            workspace={"draft_text": "林凡离开。", "beats": _PAYOFF_BEATS})
        assert [i["category"] for i in pr.craft_checks(bare)] == ["craft.payoff_no_reaction"]

    def test_disabled_by_config_copy_in_workspace(self, conn):
        cfg = NovelForgeConfig()
        cfg.payoff.enabled = False
        ctx = _ctx(conn, 3, "林凡离开。")
        assert pr.draft_brief(conn, 3, ctx.workspace, language="zh-CN", cfg=cfg) == ""
        assert ctx.workspace["payoff_reactions_cfg"]["enabled"] is False
        assert pr.craft_checks(ctx) == []

    def test_runtime_cfg_in_extra_wins_and_defaults_apply_without_any(self, conn):
        _cast(conn)
        ctx = _ctx(conn, 3, "演武场比试开始。林凡一招击败赵师兄。林凡转身离开。")
        # 没有任何配置来源（API 手动换稿 / governance 路径）：PayoffConfig() 默认值
        assert "payoff_reactions_cfg" not in ctx.workspace
        assert [i["category"] for i in pr.craft_checks(ctx)] == ["craft.payoff_no_reaction"]
        cfg = NovelForgeConfig()
        cfg.payoff.enabled = False
        ctx.extra["cfg"] = cfg
        assert pr.craft_checks(ctx) == []
        ctx.workspace["payoff_reactions_cfg"] = {"enabled": True}   # 显式 cfg 优先于黑板副本
        assert pr.craft_checks(ctx) == []

    def test_api_paths_without_cfg_match_their_own_project_config(self, conn):
        # 手动换稿用 build_project_config 构造 cfg；cfg.payoff 没有项目级设置，
        # 所以那条路径用默认值与它自己的 cfg 一致（若以后加项目级设置，此处会提醒）。
        from novelforge.control_plane.llm.runtime import build_project_config

        assert build_project_config("p", ":memory:", conn).payoff == PayoffConfig()

    def test_contract_beats_used_when_no_beat_sheet(self, conn):
        _cast(conn)
        ctx = _ctx(conn, 3, "林凡一招击败赵师兄。", beats=[], plan_context=_plan_context(3))
        assert [i["category"] for i in pr.craft_checks(ctx)] == ["craft.payoff_no_reaction"]

    def test_english_staple_reaction_is_silent(self, conn):
        # review：曾因 "grin vanished"/"hands shook" 不在词表而误报 Payoff without a reaction
        set_meta(conn, "content_language", "en-US")
        set_meta(conn, "protagonist", "Kael")
        conn.commit()
        beats = [{"seq": 1, "beat_type": "payoff_beat", "summary": "Kael disarms Dorn",
                  "witnesses": ["Dorn"]}]
        draft = ("Kael's blade stopped at Dorn's throat. Dorn's grin vanished. "
                 "His hands shook as he lowered the axe.")
        assert pr.craft_checks(_ctx(conn, 3, draft, beats)) == []

    _SOLO = [{"seq": 1, "beat_type": "setup", "summary": "林凡闭关"},
             {"seq": 2, "beat_type": "payoff_beat", "summary": "林凡突破筑基",
              "value_axis": "炼气→筑基", "witnesses": []}]
    _SOLO_DRAFT = "洞府之中，林凡盘膝而坐。三日之后，他体内灵力轰然冲破瓶颈，筑基成功。"

    def test_solo_payoff_defers_to_the_aftermath_window(self, conn):
        # review：闭关突破/独自获宝本章无人在场，不在爽点章立刻告警，交给下一章的余波规则
        _cast(conn)
        assert pr.craft_checks(_ctx(conn, 3, self._SOLO_DRAFT, self._SOLO)) == []
        # 下一章仍无任何反应 → 余波规则照常追究
        _store_contract(conn, 3, self._SOLO)
        _store_actuals(conn, 3, reactions=[])
        plain = [{"seq": 1, "beat_type": "setup", "summary": "出关", "value_axis": "静→动"}]
        issues = pr.craft_checks(_ctx(conn, 4, "林凡走出洞府，山风扑面。", plain))
        assert [i["category"] for i in issues] == ["craft.payoff_reaction_deferred"]
        assert pr.craft_checks(_ctx(conn, 4, "消息传开，众人哗然。", plain)) == []

    def test_solo_payoff_warns_immediately_without_a_window(self, conn):
        _cast(conn)
        ctx = _ctx(conn, 3, self._SOLO_DRAFT, self._SOLO)
        cfg = NovelForgeConfig()
        cfg.payoff.reaction_window_chapters = 0
        ctx.extra["cfg"] = cfg
        assert [i["category"] for i in pr.craft_checks(ctx)] == ["craft.payoff_no_reaction"]

    def test_unwitnessed_face_slap_still_warns_immediately(self, conn):
        _cast(conn)
        beats = [{"seq": 1, "beat_type": "payoff_beat", "summary": "林凡当众打脸赵师兄",
                  "value_axis": "轻视→敬畏", "witnesses": []}]
        issues = pr.craft_checks(_ctx(conn, 3, "林凡一招击败赵师兄，转身离开。", beats))
        assert [i["category"] for i in issues] == ["craft.payoff_no_reaction"]


class TestDeferredReaction:
    def test_previous_payoff_without_reactions_warns(self, conn):
        _cast(conn)
        _store_contract(conn, 4, _PAYOFF_BEATS)
        _store_actuals(conn, 4, reactions=[])
        plain = [{"seq": 1, "beat_type": "setup", "summary": "收拾残局", "value_axis": "动→静"},
                 {"seq": 2, "beat_type": "hook", "summary": "新敌人", "value_axis": "悬念↑"}]
        issues = pr.craft_checks(_ctx(conn, 5, "林凡回到住处，闭门修炼。", plain))
        assert [i["category"] for i in issues] == ["craft.payoff_reaction_deferred"]
        assert issues[0]["detail"].startswith("上一章爽点缺少反应，本章应补上余波反应")
        assert "王长老/赵师兄" in issues[0]["detail"]
        # 本章补上余波 → 静默
        assert pr.craft_checks(_ctx(conn, 5, "消息传开，王长老后悔不已。", plain)) == []
        # 群像余波不算点名在场者的反应
        assert len(pr.craft_checks(_ctx(conn, 5, "消息传开，众人哗然。", plain))) == 1

    def test_previous_payoff_with_reactions_or_unknown_actuals_is_silent(self, conn):
        _cast(conn)
        _store_contract(conn, 4, _PAYOFF_BEATS)
        plain = [{"seq": 1, "beat_type": "setup", "summary": "收拾残局", "value_axis": "动→静"}]
        assert pr.craft_checks(_ctx(conn, 5, "林凡闭门修炼。", plain)) == []   # 无 actuals = 未知
        _store_actuals(conn, 4, reactions=[{"reactor": "王长老", "reaction": "愣住"}])
        assert pr.craft_checks(_ctx(conn, 5, "林凡闭门修炼。", plain)) == []

    def test_protagonist_listed_as_witness_does_not_settle(self, conn):
        _cast(conn)
        beats = [dict(_PAYOFF_BEATS[1], witnesses=["林凡", "赵师兄"])]
        _store_contract(conn, 4, beats)
        draft = "林凡击败赵师兄。林凡愣了一下。"
        actuals = extract_chapter_actuals(4, draft, _plan_context(4, beats=beats)).actuals
        assert [r.reactor for r in actuals.reactions] == ["林凡"]   # enrich 无 DB，不知谁是主角
        _store_actuals(conn, 4, [r.model_dump(mode="json") for r in actuals.reactions])
        plain = [{"seq": 1, "beat_type": "setup", "summary": "收拾残局", "value_axis": "动→静"}]
        issues = pr.craft_checks(_ctx(conn, 5, "林凡闭门修炼。", plain))
        assert [i["category"] for i in issues] == ["craft.payoff_reaction_deferred"]
        assert "在场者 赵师兄至今没有可见反应" in issues[0]["detail"]
        report = pr.settle(conn, 4, {"draft_text": draft, "plan_context": _plan_context(4, beats=beats),
                                     "chapter_actuals": actuals}, cfg=NovelForgeConfig())
        assert report["reactions"] == 0 and report["reactors"] == []
        assert report["witnesses"] == ["赵师兄"] and report["witnesses_missing"] == ["赵师兄"]


class TestDeferredAcrossCommittedChapters:
    """窗口 ≥2：中间的非爽点章由真实管线落库（actuals 不含反应），靠重读其 L0 正文了结义务。"""

    PLAIN = [{"seq": 1, "beat_type": "setup", "summary": "消息传开", "value_axis": "静→动"}]
    WINDOW_2 = {"payoff_reactions_cfg": {"enabled": True, "reaction_window_chapters": 2}}

    @pytest.fixture
    def db(self, tmp_path):
        value = init_db(tmp_path / "novel.db")
        self.ids = _cast(value)
        self.path = tmp_path / "novel.db"
        yield value
        value.close()

    def test_plain_intermediate_chapter_settles_and_window_bounds(self, db):
        cfg = NovelForgeConfig()
        cfg.payoff.reaction_window_chapters = 2
        _store_contract(db, 3, _PAYOFF_BEATS)
        _store_contract(db, 4, self.PLAIN)
        _commit_chapter(db, self.path, 3, "演武场比试开始。林凡一招击败赵师兄。林凡转身离开。",
                        _PAYOFF_BEATS)
        assert _current_reactions(db, 3) == []
        _commit_chapter(db, self.path, 4, "消息传开，王长老后悔不已，赵师兄面如死灰。", self.PLAIN)
        assert _current_reactions(db, 4) == []          # 非爽点章：actuals 本身不记录反应
        assert pr.craft_checks(_ctx(db, 5, "林凡闭门修炼。", self.PLAIN, **self.WINDOW_2)) == []
        assert "第3章的爽点" not in pr.draft_brief(db, 5, {}, language="zh-CN", cfg=cfg)

        # 第 4 章换成没有余波的新修订 → 第 5 章仍须补
        _commit_chapter(db, self.path, 4, "林凡闭关修炼，心无旁骛。", self.PLAIN)
        issues = pr.craft_checks(_ctx(db, 5, "林凡闭门修炼。", self.PLAIN, **self.WINDOW_2))
        assert [i["category"] for i in issues] == ["craft.payoff_reaction_deferred"]
        assert issues[0]["detail"].startswith("第3章爽点缺少反应")
        brief = pr.draft_brief(db, 5, {}, language="zh-CN", cfg=cfg)
        assert "第3章的爽点（在场者 王长老、赵师兄）还没有写出可见反应" in brief
        # 本章补上 → 静默；窗口 1（默认）：第 5 章已超出第 3 章的窗口
        assert pr.craft_checks(_ctx(db, 5, "王长老面色铁青，悔不当初。", self.PLAIN,
                                    **self.WINDOW_2)) == []
        assert pr.craft_checks(_ctx(db, 5, "林凡闭门修炼。", self.PLAIN)) == []

    def test_alias_reaction_in_payoff_chapter_is_not_an_obligation(self, db):
        db.execute("INSERT INTO entity_aliases(id, entity_id, alias) VALUES('a1', ?, '王老怪')",
                   (self.ids["王长老"],))
        _store_contract(db, 4, _PAYOFF_BEATS)
        draft = "林凡一招击败赵师兄。王老怪愣住了。"
        _commit_chapter(db, self.path, 4, draft, _PAYOFF_BEATS)
        assert _current_reactions(db, 4) == []          # enrich 无 DB：不认识别名
        assert pr.craft_checks(_ctx(db, 4, draft)) == []  # 第 4 章的检查认识别名
        # 下一章与第 4 章的检查同口径：重读第 4 章正文（带别名）→ 不欠余波
        assert pr.craft_checks(_ctx(db, 5, "林凡闭门修炼。", self.PLAIN)) == []


class TestEnglishProject:
    BEATS = [{"seq": 1, "beat_type": "payoff_beat", "summary": "Lin Fan defeats Brother Zhao",
              "value_axis": "scorn→awe", "witnesses": ["Elder Wang"]},
             {"seq": 2, "beat_type": "hook", "summary": "A shadow moves"}]
    PLAIN = [{"seq": 1, "beat_type": "setup", "summary": "Lin Fan returns home"}]

    @pytest.fixture(autouse=True)
    def _english_cast(self, conn):
        for name in ("Lin Fan", "Elder Wang"):
            _entity(conn, name)
        set_meta(conn, "protagonist", "Lin Fan")
        set_meta(conn, "content_language", "en-US")
        conn.commit()

    @staticmethod
    def _no_cjk(*texts: str) -> bool:
        return not any(pr._CJK_RE.search(t) for t in texts)

    def test_crowd_only_warning_is_english(self, conn):
        draft = "Lin Fan defeats Brother Zhao. The crowd gasped."
        issues = pr.craft_checks(_ctx(conn, 3, draft, self.BEATS))
        assert [i["category"] for i in issues] == ["craft.payoff_crowd_only"]
        issue = issues[0]
        assert issue["detail"].startswith("Only the crowd reacts to the payoff (the crowd)")
        assert "Elder Wang" in issue["detail"] and "Give Elder Wang" in issue["fix"]
        assert issue["span"] == "The crowd gasped."
        assert self._no_cjk(issue["detail"], issue["fix"], issue["span"])

    def test_deferred_warning_and_brief_are_english(self, conn):
        _store_contract(conn, 4, self.BEATS)
        _store_actuals(conn, 4, reactions=[])
        issues = pr.craft_checks(_ctx(conn, 5, "Lin Fan meditated alone.", self.PLAIN))
        assert [i["category"] for i in issues] == ["craft.payoff_reaction_deferred"]
        issue = issues[0]
        assert issue["detail"].startswith("The previous chapter's payoff got no reaction")
        assert "witnesses Elder Wang" in issue["detail"] and "Elder Wang" in issue["fix"]
        assert self._no_cjk(issue["detail"], issue["fix"])
        brief = pr.draft_brief(conn, 5, {}, language="en-US", cfg=NovelForgeConfig())
        assert "- Chapter 4's payoff (witnesses: Elder Wang) never showed a visible reaction" in brief
        assert self._no_cjk(brief)
        # the aftermath is written in this chapter → silent
        assert pr.craft_checks(_ctx(conn, 5, "Hearing the news, Elder Wang went pale.",
                                    self.PLAIN)) == []

    def test_settle_report_on_english_draft(self, conn):
        draft = "Lin Fan defeats Brother Zhao. Elder Wang went pale."
        context = _plan_context(5, beats=self.BEATS)
        actuals = extract_chapter_actuals(5, draft, context).actuals
        ws = {"draft_text": draft, "plan_context": context, "chapter_actuals": actuals}
        report = pr.settle(conn, 5, ws, cfg=NovelForgeConfig())
        assert report == {
            "payoff_beats": 1, "reactions": 1, "witnesses": ["Elder Wang"],
            "reactors": ["Elder Wang"], "witnesses_missing": [], "source": "actuals",
        }
        ws = {"draft_text": "Lin Fan defeats Brother Zhao. The crowd fell silent.",
              "beats": self.BEATS, "plan_context": PlanContext(target_chapter=5)}
        report = pr.settle(conn, 5, ws, cfg=NovelForgeConfig())
        assert report["source"] == "draft" and report["reactors"] == ["the crowd"]
        assert report["witnesses_missing"] == ["Elder Wang"]
        assert self._no_cjk(json.dumps(report, ensure_ascii=False))


class TestCraftSkillIntegration:
    def test_issue_surfaces_as_warn_and_does_not_block(self, conn):
        from novelforge.skills.craft_check_skill import CraftCheckSkill

        _cast(conn)
        ctx = _ctx(conn, 3, "演武场比试开始。林凡一招击败赵师兄。黑影一闪。")
        result = CraftCheckSkill().run(ctx)
        ours = [i for i in ctx.workspace["craft_issues"] if i["check"] == "payoff_reaction"]
        assert len(ours) == 1
        assert ours[0]["severity"] == "warn" and ours[0]["source"] == "craft"
        assert ours[0]["category"] == "craft.payoff_no_reaction"
        assert result.ok


# ── 4. payoff_binding：打脸需要在场者（只 warn）────────────────────────────────

class TestPayoffBinding:
    def test_face_slap_without_witnesses_warns_bilingually(self):
        zh = check_payoff_binding(
            [{"beat_type": "payoff_beat", "summary": "当众打脸", "value_axis": "轻视→敬畏"}],
            [], 3, None,
        )
        assert [(i.payoff_type, i.severity) for i in zh] == [("face_slap", "warn")]
        assert "在场者" in zh[0].detail
        en = check_payoff_binding(
            [{"beat_type": "payoff_beat", "summary": "A public comeuppance", "value_axis": "scorn"}],
            [], 3, None,
        )
        assert [(i.payoff_type, i.severity) for i in en] == [("face_slap", "warn")]
        assert "witnesses" in en[0].detail and not pr._CJK_RE.search(en[0].detail)

    def test_counterattack_does_not_demand_witnesses(self):
        beats = [{"beat_type": "payoff_beat", "summary": "林凡反击成功，一招制敌",
                  "value_axis": "守→攻"}]
        assert check_payoff_binding(beats, [], 3, None) == []

    def test_face_slap_with_witnesses_passes_and_others_unchanged(self):
        beats = [
            {"beat_type": "payoff_beat", "summary": "当众打脸", "witnesses": "王长老、赵师兄"},
            {"beat_type": "payoff_beat", "summary": "突破", "value_axis": "power"},
        ]
        issues = check_payoff_binding(beats, [], 3, None)
        assert [(i.beat_index, i.payoff_type, i.severity) for i in issues] == [
            (1, "power_up", "block"),
        ]


# ── 5. draft_brief：三段式提醒 + 余波 + 信息差 ─────────────────────────────────

class TestDraftBrief:
    def _workspace(self, chapter=5, **contract):
        base = {"chapter": chapter, "beats": _PAYOFF_BEATS, "must_include_entities": ["王长老"]}
        base.update(contract)
        return {"plan_context": PlanContext(target_chapter=chapter, current_contract=base)}

    def test_payoff_reminder_names_witnesses_without_protagonist(self, conn):
        _cast(conn)
        beats = [dict(_PAYOFF_BEATS[1], witnesses=["王长老", "林凡"])]
        brief = pr.draft_brief(conn, 5, {"beats": beats}, language="zh-CN",
                               cfg=NovelForgeConfig())
        assert brief.startswith("## 爽点三段式（动作→在场者反应→收获重要性展示）")
        assert "第2拍「林凡一招击败赵师兄」是爽点" in brief
        assert "在场者 王长老的可见反应" in brief and "林凡的可见反应" not in brief

    def test_info_gap_from_knowledge_edges(self, conn):
        ids = _cast(conn)
        lin, wang, zhao = ids["林凡"], ids["王长老"], ids["赵师兄"]
        _knowledge(conn, lin, "魔尊的真实身份", "knows", 2, secrecy_level="top_secret")
        _knowledge(conn, wang, "魔尊的真实身份", "unaware", 1)
        _knowledge(conn, lin, "玉佩来历", "knows", 1)
        _knowledge(conn, wang, "玉佩来历", "misinformed", 1)
        _knowledge(conn, lin, "宗门大比内定", "knows", 1, public_from_chapter=3)   # 已公开
        _knowledge(conn, wang, "宗门大比内定", "unaware", 1)
        _knowledge(conn, lin, "禁地入口", "knows", 1)                            # 契约禁止揭示
        _knowledge(conn, wang, "禁地入口", "unaware", 1)
        _knowledge(conn, lin, "新秘密", "knows", 5)                              # 本章才得知
        _knowledge(conn, wang, "新秘密", "unaware", 1)
        _knowledge(conn, zhao, "魔尊的真实身份", "unaware", 1)                    # 不在本章出场
        conn.commit()
        ws = self._workspace(beats=[], must_not_reveal=["禁地入口"])
        brief = pr.draft_brief(conn, 5, ws, language="zh-CN", cfg=NovelForgeConfig())
        assert brief.splitlines() == [
            "## 信息差（可用于当众揭晓/打脸）",
            "- 信息差机会：王长老 还不知道「魔尊的真实身份」（主角已知）——可安排当众揭晓/打脸",
            "- 信息差机会：王长老 对「玉佩来历」仍有误判（主角已知真相）——可安排当众揭晓/打脸",
        ]
        cfg = NovelForgeConfig()
        cfg.payoff.info_gap_suggestions = 1
        assert len(pr.draft_brief(conn, 5, ws, language="zh-CN", cfg=cfg).splitlines()) == 2
        # 目标后来得知（knows 覆盖 unaware）→ 不再是信息差
        _knowledge(conn, wang, "魔尊的真实身份", "knows", 3)
        _knowledge(conn, wang, "玉佩来历", "knows", 4)
        conn.commit()
        assert pr.draft_brief(conn, 5, ws, language="zh-CN", cfg=NovelForgeConfig()) == ""

    def test_info_gap_falls_back_to_recall_when_contract_names_do_not_resolve(self, conn):
        ids = _cast(conn)
        _knowledge(conn, ids["林凡"], "魔尊的真实身份", "knows", 1)
        _knowledge(conn, ids["王长老"], "魔尊的真实身份", "unaware", 1)
        conn.commit()

        class Recall:
            entities = [{"id": ids["王长老"], "canonical_name": "王长老", "entity_type": "character"}]

        ws = self._workspace(beats=[], must_include_entities=["王老头"])   # 写法对不上实体/别名
        ws["recall_pack"] = Recall()
        brief = pr.draft_brief(conn, 5, ws, language="zh-CN", cfg=NovelForgeConfig())
        assert "- 信息差机会：王长老 还不知道「魔尊的真实身份」（主角已知）" in brief

    def test_english_brief_and_recall_fallback(self, conn):
        lin = _entity(conn, "Lin Fan")
        wang = _entity(conn, "Elder Wang")
        set_meta(conn, "protagonist", "Lin Fan")
        _knowledge(conn, lin, "the demon lord's identity", "knows", 1)
        _knowledge(conn, wang, "the demon lord's identity", "unaware", 1)
        conn.commit()

        class Recall:
            entities = [{"id": wang, "canonical_name": "Elder Wang", "entity_type": "character"},
                        {"id": lin, "canonical_name": "Lin Fan", "entity_type": "character"}]

        beats = [{"seq": 1, "beat_type": "payoff_beat", "summary": "Lin Fan wins the duel",
                  "witnesses": ["Elder Wang"]}]
        brief = pr.draft_brief(conn, 4, {"beats": beats, "recall_pack": Recall()},
                               language="en-US", cfg=NovelForgeConfig())
        assert "## Payoff in three steps" in brief
        assert "visible reactions from the witnesses Elder Wang" in brief
        assert ("- Information-gap opportunity: Elder Wang still does not know "
                "“the demon lord's identity” (the protagonist does)") in brief
        assert not pr._CJK_RE.search(brief)

    def test_pending_aftermath_line_and_no_routing_markers(self, conn):
        _cast(conn)
        _store_contract(conn, 4, _PAYOFF_BEATS)
        _store_actuals(conn, 4, reactions=[])
        brief = pr.draft_brief(conn, 5, {}, language="zh-CN", cfg=NovelForgeConfig())
        assert "第4章的爽点（在场者 王长老、赵师兄）还没有写出可见反应" in brief
        for marker in _ROUTING_MARKERS:
            assert marker not in brief

    def test_empty_when_nothing_applies(self, conn):
        assert pr.draft_brief(conn, 1, {}, language="zh-CN", cfg=NovelForgeConfig()) == ""
        assert pr.draft_brief(None, 3, None, language="zh-CN", cfg=None) == ""


# ── 6. settle：覆盖率报告（确定性、只读）─────────────────────────────────────

class TestSettleReport:
    def test_report_reads_persisted_actuals(self, conn):
        draft = "林凡一招击败赵师兄。王长老脸色大变。"
        actuals = extract_chapter_actuals(5, draft, _plan_context()).actuals
        ws = {"draft_text": draft, "plan_context": _plan_context(), "chapter_actuals": actuals,
              "beats": _PAYOFF_BEATS}
        report = pr.settle(conn, 5, ws, gateway=None, cfg=NovelForgeConfig())
        assert report == {
            "payoff_beats": 1, "reactions": 1, "witnesses": ["王长老", "赵师兄"],
            "reactors": ["王长老"], "witnesses_missing": ["赵师兄"], "source": "actuals",
        }
        assert pr.settle(conn, 5, ws, gateway=None, cfg=NovelForgeConfig()) == report

    def test_planner_beats_fall_back_to_direct_detection(self, conn):
        draft = "林凡一招击败赵师兄。赵师兄咬牙切齿。"
        ws = {"draft_text": draft, "plan_context": PlanContext(target_chapter=5),
              "chapter_actuals": ChapterActuals(chapter=5), "beats": _PAYOFF_BEATS}
        report = pr.settle(conn, 5, ws, cfg=NovelForgeConfig())
        assert report["source"] == "draft" and report["reactors"] == ["赵师兄"]
        assert report["witnesses_missing"] == ["王长老"]

    def test_protagonist_listed_as_witness_is_not_missing(self, conn):
        _cast(conn)
        beats = [dict(_PAYOFF_BEATS[1], witnesses=["王长老", "林凡"])]
        ws = {"draft_text": "林凡出手。王长老愣住了。", "beats": beats,
              "plan_context": PlanContext(target_chapter=5)}
        report = pr.settle(conn, 5, ws, cfg=NovelForgeConfig())
        assert report["witnesses"] == ["王长老"] and report["witnesses_missing"] == []

    def test_no_payoff_and_disabled(self, conn):
        report = pr.settle(conn, 5, {"draft_text": "平稳。", "beats": []}, cfg=NovelForgeConfig())
        assert report["payoff_beats"] == 0 and report["witnesses_missing"] == []
        cfg = NovelForgeConfig()
        cfg.payoff.enabled = False
        assert pr.settle(conn, 5, {}, cfg=cfg) == {"skipped": "disabled"}


# ── 7. 端到端：提示块位置、craft 告警、actuals 与结算报告 ─────────────────────

def _orchestrator(factory, *, window: int = 1):
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway
    from novelforge.control_plane.orchestrator import Orchestrator
    from novelforge.control_plane.skill_registry import SkillRegistry
    from novelforge.skills import register_default_skills

    fake = FakeProvider(factory=factory)
    gateway = LLMGateway(fake, BudgetLedger(max_tokens=10_000_000, max_usd=100.0,
                                            max_revise_rounds=100))
    registry = SkillRegistry()
    register_default_skills(registry)
    cfg = NovelForgeConfig(project_id="f5")
    cfg.provider.provider = "fake"
    cfg.recall.enable_summaries = False
    cfg.settle.enabled = False
    cfg.payoff.reaction_window_chapters = window
    return Orchestrator(gateway, registry, cfg), fake


class TestPipeline:
    @pytest.fixture
    def db(self, tmp_path):
        value = init_db(tmp_path / "novel.db")
        ids = _cast(value)
        value.execute(
            "INSERT INTO chapter_cards(id, chapter, goal, status, locked, must_include_entities_json)"
            " VALUES('card-2', 2, '演武场比试', 'planned', 1, ?)",
            (json.dumps(["王长老"], ensure_ascii=False),),
        )
        for beat in _PAYOFF_BEATS:
            value.execute(
                "INSERT INTO beats(id, chapter, seq, beat_type, summary, value_axis,"
                " constraint_level, witnesses_json) VALUES(?,?,?,?,?,?,?,?)",
                (new_id("beat"), 2, beat["seq"], beat["beat_type"], beat["summary"],
                 beat["value_axis"], "hard" if beat["seq"] == 2 else "soft",
                 json.dumps(beat.get("witnesses") or [], ensure_ascii=False)),
            )
        _knowledge(value, ids["林凡"], "魔尊的真实身份", "knows", 1)
        _knowledge(value, ids["王长老"], "魔尊的真实身份", "unaware", 1)
        value.commit()
        yield value
        value.close()

    @staticmethod
    def _factory(body: str):
        planned = json.dumps([
            {"beat_type": "setup", "summary": "开场", "value_axis": "平静→波澜"},
            {"beat_type": "turn", "summary": "转折", "value_axis": "守→攻"},
            {"beat_type": "hook", "summary": "章末悬念", "value_axis": "悬念↑"},
        ], ensure_ascii=False)

        def factory(messages, model="", temperature=1.0):
            user = str(messages[-1].content) if messages else ""
            if "规划任务" in user:
                return planned
            if "本章任务" in user:
                return f"```draft\n{body}\n```\n```proposals\n[]\n```"
            return "[]"
        return factory

    def test_brief_follows_contract_outside_stable_prefix(self, db):
        body = ("演武场比试开始。林凡一招击败赵师兄。林凡转身离开，黑影一闪。\n" * 20).strip()
        orch, fake = _orchestrator(self._factory(body))
        outcome = orch.generate_chapter(2, db, chapter_goal="")
        assert outcome.ok, outcome.error

        call = next(c for c in fake.calls if "本章任务" in str(c["messages"][-1].content))
        user = str(call["messages"][-1].content)
        prefix = call["cache_hint"].user_prefix_chars
        contract_at = user.index("## 本章硬契约")
        brief_at = user.index("## 爽点三段式")
        gap_at = user.index("- 信息差机会：王长老 还不知道「魔尊的真实身份」")
        assert 0 < prefix <= contract_at < brief_at < gap_at < user.index("## 本章任务")
        assert "爽点三段式" not in user[:prefix] and "信息差" not in user[:prefix]

        ours = [i for i in outcome.issues if i.get("check") == "payoff_reaction"]
        assert [(i["severity"], i["category"]) for i in ours] == [
            ("warn", "craft.payoff_no_reaction"),
        ]
        detail = json.loads(db.execute(
            "SELECT detail_json FROM pipeline_run WHERE run_id=?", (outcome.run_id,),
        ).fetchone()["detail_json"])
        report = detail["extensions"]["payoff_reactions"]
        assert report["payoff_beats"] == 1 and report["reactions"] == 0
        assert report["witnesses_missing"] == ["王长老", "赵师兄"]

    def test_reactions_reach_chapter_actuals(self, db):
        body = ("演武场比试开始。林凡一招击败赵师兄。王长老脸色大变，赵师兄面如死灰。黑影一闪。\n"
                * 20).strip()
        orch, _ = _orchestrator(self._factory(body))
        outcome = orch.generate_chapter(2, db, chapter_goal="")
        assert outcome.ok, outcome.error
        assert not [i for i in outcome.issues if i.get("check") == "payoff_reaction"]
        payload = json.loads(db.execute(
            "SELECT payload_json FROM chapter_actuals WHERE chapter=2 AND status='current'"
        ).fetchone()["payload_json"])
        reactors = {r["reactor"] for r in payload["reactions"]}
        assert {"王长老", "赵师兄"} <= reactors
        assert all(r["beat_seq"] == 2 for r in payload["reactions"])

    def test_planner_gets_info_gap_brief_without_contract(self, db):
        orch, fake = _orchestrator(self._factory("林凡走进演武场，众人侧目。" * 30))
        outcome = orch.generate_chapter(3, db, chapter_goal="")
        assert outcome.ok, outcome.error
        call = next(c for c in fake.calls if "规划任务" in str(c["messages"][-1].content))
        user = str(call["messages"][-1].content)
        prefix = call["cache_hint"].user_prefix_chars
        line = "- 信息差机会：王长老 还不知道「魔尊的真实身份」（主角已知）——可安排当众揭晓/打脸"
        assert prefix < user.index(line) < user.index("## 规划任务")
        assert "爽点三段式" not in user        # 无契约爽点：只给信息差
        assert not [i for i in outcome.issues if i.get("check") == "payoff_reaction"]


class TestDeferredPipelineWindow:
    """reaction_window_chapters=2，经编排器连写第 3（爽点无反应）→4（普通章）→5 章。"""

    @pytest.fixture
    def db(self, tmp_path):
        value = init_db(tmp_path / "novel.db")
        _cast(value)
        value.execute(
            "INSERT INTO chapter_cards(id, chapter, goal, status, locked)"
            " VALUES('card-3', 3, '演武场比试', 'planned', 1)"
        )
        for beat in _PAYOFF_BEATS:
            value.execute(
                "INSERT INTO beats(id, chapter, seq, beat_type, summary, value_axis, witnesses_json)"
                " VALUES(?,?,?,?,?,?,?)",
                (new_id("beat"), 3, beat["seq"], beat["beat_type"], beat["summary"],
                 beat["value_axis"], json.dumps(beat.get("witnesses") or [], ensure_ascii=False)),
            )
        value.commit()
        yield value
        value.close()

    @pytest.mark.parametrize("aftermath,expected", [
        ("消息传开，王长老后悔不已，赵师兄面如死灰。林凡闭关修炼。", []),
        ("林凡闭关修炼，心无旁骛。", ["craft.payoff_reaction_deferred"]),
    ])
    def test_plain_chapter_aftermath_settles_payoff(self, db, aftermath, expected):
        holder = {"body": ""}
        planned = json.dumps([
            {"beat_type": "setup", "summary": "开场", "value_axis": "平静→波澜"},
            {"beat_type": "hook", "summary": "章末悬念", "value_axis": "悬念↑"},
        ], ensure_ascii=False)

        def factory(messages, model="", temperature=1.0):
            user = str(messages[-1].content) if messages else ""
            if "规划任务" in user:
                return planned
            if "本章任务" in user:
                return f"```draft\n{holder['body']}\n```\n```proposals\n[]\n```"
            return "[]"

        orch, fake = _orchestrator(factory, window=2)
        for chapter, body in ((3, "演武场比试开始。林凡一招击败赵师兄。林凡转身离开，黑影一闪。"),
                              (4, aftermath)):
            holder["body"] = ((body + "\n") * 20).strip()
            outcome = orch.generate_chapter(chapter, db, chapter_goal="")
            assert outcome.ok, outcome.error
        assert json.loads(db.execute(
            "SELECT payload_json FROM chapter_actuals WHERE chapter=4 AND status='current'"
        ).fetchone()["payload_json"])["reactions"] == []

        seen = len(fake.calls)
        holder["body"] = ("林凡出关，走向山门。\n" * 20).strip()
        outcome = orch.generate_chapter(5, db, chapter_goal="")
        assert outcome.ok, outcome.error
        ours = [i for i in outcome.issues if i.get("check") == "payoff_reaction"]
        assert [i["category"] for i in ours] == expected
        draft_prompt = next(str(c["messages"][-1].content) for c in fake.calls[seen:]
                            if "本章任务" in str(c["messages"][-1].content))
        assert ("第3章的爽点" in draft_prompt) == bool(expected)
