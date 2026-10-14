"""v22/F1 读者承诺账本：helpers / craft checks / draft brief / settle / API / foreshadow_settle。

全部 FakeProvider 或纯函数，无网络。
"""
from __future__ import annotations

import json
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from novelforge.config import NovelForgeConfig, PromiseConfig
from novelforge.craft import promise_ledger as pl
from novelforge.planning.context import PlanContext

_CJK = re.compile(r"[一-鿿]")
_ROUTING_MARKERS = (
    "规划任务", "## Planning Task", "本章任务", "## Chapter Task", "草稿：", "### 候选",
    "本章目标：", "Chapter goal: ", "一致性问题", "修订补丁任务", "润色补丁任务", "工艺问题",
    "未解伏笔", "章正文：", "各章摘要", "卷《", "章正文如下：", "场景原文",
)


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def conn():
    from novelforge.db.connection import init_db_from_conn

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db_from_conn(c)
    yield c
    c.close()


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_DATA", str(tmp_path))
    monkeypatch.setenv("NOVELFORGE_PROVIDER", "fake")
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

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def project(client):
    resp = client.post("/v1/projects", json={"name": "承诺账本", "genre": "xuanhuan"})
    assert resp.status_code == 201, resp.text
    return resp.json()["project_id"]


def _open_conn(project_id):
    from novelforge.app.deps import get_registry

    return get_registry().open_conn(project_id)


def _fs(conn, fs_id, label, *, desc="", state="planted", planted=1, mentioned=None,
        advanced=None, line_type="long", visibility="reader", parent=None,
        remind_every=None, importance=3, paid_off=None):
    conn.execute(
        "INSERT INTO foreshadow(id, label, description, state, planted_chapter,"
        " last_mentioned_chapter, last_advanced_chapter, line_type, visibility,"
        " parent_id, remind_every, importance, paid_off_chapter)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fs_id, label, desc or label, state, planted, mentioned, advanced, line_type,
         visibility, parent, remind_every, importance, paid_off),
    )
    conn.commit()


def _log(conn, fs_id, chapter, action="mention"):
    from novelforge.ids import new_id

    conn.execute(
        "INSERT INTO foreshadow_log(id, foreshadow_id, chapter, action) VALUES(?,?,?,?)",
        (new_id("fsl"), fs_id, chapter, action),
    )
    conn.commit()


def _ctx(conn, chapter, draft, *, actions=None, language="zh-CN", volume_end=None,
         cfg=None):
    from novelforge.control_plane.skill_base import SkillContext

    workspace = {
        "draft_text": draft,
        "writing_profile": {"content_language": language},
    }
    if actions is not None or volume_end is not None:
        workspace["plan_context"] = PlanContext(
            target_chapter=chapter,
            current_contract={"chapter": chapter, "promise_actions": actions or []},
            current_volume=({"volume_no": 1, "start_chapter": 1, "end_chapter": volume_end}
                            if volume_end else None),
        )
    if cfg is not None:
        from dataclasses import asdict
        workspace["promise_config"] = asdict(cfg)
    return SkillContext(
        project_id="p", target_chapter=chapter, mode="human_gate",
        as_of_chapter=chapter - 1, budget=None, llm=None, conn=conn, workspace=workspace,
    )


def _checks(issues, name):
    return [i for i in issues if i["check"] == name]


_PLAIN = "陆天走进山门，风很冷，他一言不发。"


# ── 纯函数 helpers ────────────────────────────────────────────────────────────

class TestHelpers:
    def test_effective_remind_every_defaults_and_override(self):
        cfg = PromiseConfig()
        assert pl.effective_remind_every({"line_type": "long"}, cfg) == 5
        assert pl.effective_remind_every({"line_type": "short"}, cfg) == 2
        assert pl.effective_remind_every({"line_type": "micro"}, cfg) == 1
        assert pl.effective_remind_every({"line_type": "short", "remind_every": 7}, cfg) == 7
        cfg8 = PromiseConfig(default_remind_every=8)
        assert pl.effective_remind_every({"line_type": "short"}, cfg8) == 4
        assert pl.dormant_after({"line_type": "long"}, cfg) == 10

    def test_touch_and_dormancy(self):
        cfg = PromiseConfig()
        row = {"state": "reinforced", "planted_chapter": 1, "last_mentioned_chapter": 4,
               "last_advanced_chapter": 6, "line_type": "long", "visibility": "reader"}
        assert pl.last_touch_chapter(row) == 6
        assert pl.chapters_since_touch(row, 10) == 4
        assert pl.chapters_since_touch(row, 3) == 0      # 重写早期章节不出负数
        assert not pl.is_dormant(row, 16, cfg)           # 10 章：恰好不算
        assert pl.is_dormant(row, 17, cfg)
        assert pl.is_due_for_reminder(row, 11, cfg) and not pl.is_due_for_reminder(row, 10, cfg)
        assert not pl.is_dormant({**row, "visibility": "hidden"}, 30, cfg)
        assert not pl.is_dormant({**row, "line_type": "micro"}, 30, cfg)
        assert not pl.is_dormant({**row, "state": "paid_off"}, 30, cfg)

    def test_dormancy_rank_relative_to_cadence(self):
        cfg = PromiseConfig()
        long_row = {"id": "a", "planted_chapter": 1, "line_type": "long", "importance": 3}
        short_row = {"id": "b", "planted_chapter": 8, "line_type": "short", "importance": 3}
        rows = sorted([long_row, short_row], key=lambda r: pl.dormancy_rank(r, 14, cfg))
        assert [r["id"] for r in rows] == ["b", "a"]     # 6/2 > 13/5

    def test_promise_kind_vocabulary_is_soft(self):
        assert pl.normalize_promise_kind("MICE-Inquiry") == "mice_inquiry"
        assert pl.normalize_promise_kind(" vow ") == "vow"
        assert pl.normalize_promise_kind("prophecy") == "prophecy"
        assert pl.normalize_promise_kind("  ") is None
        assert pl.promise_kind_category("prophecy") == "other"
        assert pl.promise_kind_category("Info Gap") == "info_gap"

    def test_draft_matcher_cjk(self):
        m = pl.DraftMatcher("陆天想起了灭门那夜。那灭门的真凶究竟是谁？他握紧了剑。")
        assert m.mentions("灭门真凶")                     # bigram ≥ 0.5（同句）
        assert m.find("灭门真凶") == "那灭门的真凶究竟是谁"
        assert m.mentions("血仇", "灭门真凶")             # 短描述作为关键短语
        assert not m.mentions("血仇", "当年那场牵连三大宗门、至今无人敢提起的灭门惨案的幕后真凶")
        assert not m.mentions("血仇")
        assert not m.mentions("剑")                       # 单字 label 不匹配
        assert pl.DraftMatcher("师父说：「血 仇，必报」").mentions("血仇")   # 忽略空白/标点

    def test_draft_matcher_english(self):
        m = pl.DraftMatcher("Mara counted the coins. The debt of blood was still unpaid! "
                            "She started walking.")
        assert m.mentions("The Blood Debt")               # 内容词 ≥ 50% 且 ≥ 2 个
        assert not m.mentions("Art")                      # 整词匹配，start ≠ art
        assert m.mentions("coins")
        assert not m.mentions("Blood Oath of the North")
        assert pl.label_in_text("blood debts", "Old blood debt never fades.")
        # 两个内容词都在同一句里：算作对 "The Broken Sword" 的呼应（有意保留）
        assert pl.DraftMatcher("A sword lay broken in the mud.").mentions("The Broken Sword")
        assert not pl.DraftMatcher("His name was Tom.").mentions("The Name of His Enemy")

    def test_draft_matcher_cjk_rejects_function_bigrams(self):
        # 评审探针：只靠虚字二元组（的名/名字、人身/身份）重合的句子不算提到了承诺
        assert not pl.DraftMatcher("他的名字叫张三，是个铁匠。").mentions("仇人的名字")
        assert not pl.DraftMatcher("此人身份不明。").mentions("黑袍人身份")
        # 实词片段各自出现、彼此靠近 → 仍算
        assert pl.DraftMatcher("那黑袍的身份终于有了眉目。").mentions("黑袍人身份")
        assert pl.DraftMatcher("那个神秘人究竟什么来历？").mentions("神秘人的来历")
        assert pl.DraftMatcher("灭门之仇，真凶未明。").mentions("灭门真凶")
        # 实词片段在同一长句里相隔太远 → 不算
        assert not pl.DraftMatcher("他拔出青铜长剑，心里藏着一个秘密。").mentions("青铜剑的秘密")
        # 精确模式不走模糊兜底
        m = pl.DraftMatcher("那灭门的真凶究竟是谁？")
        assert m.mentions("灭门真凶") and not m.mentions("灭门真凶", fuzzy=False)
        assert pl.DraftMatcher("他想起血仇。").find("血仇", fuzzy=False) == "他想起血仇"

    def test_content_units_between(self, conn):
        conn.execute("INSERT INTO chapter_cards(id, chapter, word_count) VALUES('c1', 1, 4000)")
        conn.execute(
            "INSERT INTO draft_index(id, chapter, revision_round, file_path, sha256, word_count)"
            " VALUES('d2a', 2, 0, 'x', 'h', 1000), ('d2b', 2, 1, 'y', 'h', 2500)")
        conn.commit()
        # 1: card 4000；2: 最新修订 2500；3: 兜底 3000
        assert pl.content_units_between(conn, 1, 4, language="zh-CN") == 9500
        assert pl.content_units_between(conn, 3, 5, language="en-US") == 4000
        assert pl.content_units_between(conn, 5, 5) == 0

    def test_touch_count_distinct_chapters(self, conn):
        _fs(conn, "fs1", "血仇", planted=2, mentioned=7)
        _log(conn, "fs1", 2, "plant")
        _log(conn, "fs1", 2, "mention")      # 埋设章不重复计
        _log(conn, "fs1", 4, "mention")
        _log(conn, "fs1", 4, "advance")      # 同章去重
        _log(conn, "fs1", 9, "mention")      # 不早于 before_chapter → 不计
        row = pl.fetch_promises(conn, open_only=False)[0]
        assert pl.touch_count(conn, row, 9) == 3      # 埋设 + 第4章 + 第7章（last_mentioned）

    def test_fetch_promises_filters_and_defaults(self, conn):
        _fs(conn, "a", "甲", planted=1)
        _fs(conn, "b", "乙", planted=5, state="paid_off")
        _fs(conn, "c", "丙", planted=9)
        rows = pl.fetch_open_promises(conn, as_of_chapter=6)
        assert [r["id"] for r in rows] == ["a"]
        assert rows[0]["line_type"] == "long" and rows[0]["visibility"] == "reader"
        assert len(pl.fetch_promises(conn, open_only=False)) == 3
        bare = sqlite3.connect(":memory:")     # 无表的库：容错为空
        try:
            assert pl.fetch_open_promises(bare) == []
        finally:
            bare.close()


# ── craft checks ─────────────────────────────────────────────────────────────

class TestCraftChecks:
    def test_fresh_ledger_is_silent(self, conn):
        for chapter in (1, 3, 12):
            assert pl.craft_checks(_ctx(conn, chapter, _PLAIN)) == []

    def test_tolerates_bare_context(self):
        assert pl.craft_checks(object()) == []

    def test_dormant_warn_zh(self, conn):
        _fs(conn, "fs_old", "血仇", desc="灭门真凶仍未现身", planted=1, mentioned=1)
        issues = pl.craft_checks(_ctx(conn, 12, _PLAIN))
        dormant = _checks(issues, "promise_dormant")
        assert len(dormant) == 1
        assert dormant[0]["severity"] == "warn"
        assert dormant[0]["category"] == "craft.promise_dormant"
        assert "血仇" in dormant[0]["detail"] and "11 章" in dormant[0]["detail"]
        assert "自然呼应" in dormant[0]["fix"]

    def test_dormant_silent_when_mentioned_hidden_or_recent(self, conn):
        _fs(conn, "fs_old", "血仇", planted=1)
        _fs(conn, "fs_hidden", "幕后黑手", planted=1, visibility="hidden")
        _fs(conn, "fs_recent", "断剑", planted=1, mentioned=8)
        assert pl.craft_checks(_ctx(conn, 12, "他想起血仇，夜不能寐。")) == []

    def test_dormant_not_silenced_by_function_bigrams(self, conn):
        _fs(conn, "fs_name", "仇人的名字", planted=1)
        _fs(conn, "fs_robe", "黑袍人身份", planted=1)
        issues = pl.craft_checks(_ctx(conn, 12, "他的名字叫张三。此人身份不明。"))
        assert len(_checks(issues, "promise_dormant")) == 2

    def test_dormant_cap_and_order(self, conn):
        for i in range(5):
            _fs(conn, f"fs{i}", f"承诺{'甲乙丙丁戊'[i]}", planted=1 + i)
        issues = _checks(pl.craft_checks(_ctx(conn, 20, _PLAIN)), "promise_dormant")
        assert len(issues) == 3
        assert "承诺甲" in issues[0]["detail"] and "承诺丙" in issues[2]["detail"]

    def test_dormant_english_variant(self, conn):
        _fs(conn, "fs_en", "The Blood Debt", desc="who ordered the massacre", planted=1)
        issues = pl.craft_checks(_ctx(conn, 12, "Mara walked to the gate.", language="en-US"))
        dormant = _checks(issues, "promise_dormant")
        assert len(dormant) == 1
        assert 'Reader promise "The Blood Debt"' in dormant[0]["detail"]
        assert not _CJK.search(dormant[0]["detail"] + dormant[0]["fix"])

    def test_short_line_overdue(self, conn):
        _fs(conn, "fs_s", "断剑之谜", planted=1, line_type="short")   # 也已休眠
        issues = pl.craft_checks(_ctx(conn, 12, _PLAIN))
        overdue = _checks(issues, "promise_short_overdue")
        assert len(overdue) == 1 and "33,000" in overdue[0]["detail"]
        assert not _checks(issues, "promise_dormant")     # 同一承诺不重复告警
        # 正文推进了它 → 静默
        assert pl.craft_checks(_ctx(conn, 12, "断剑之谜终于有了线索。")) == []

    def test_short_line_uses_recorded_counts(self, conn):
        _fs(conn, "fs_s", "断剑之谜", planted=1, mentioned=10, line_type="short")
        for ch in range(1, 12):
            conn.execute("INSERT INTO chapter_cards(id, chapter, word_count) VALUES(?,?,2000)",
                         (f"c{ch}", ch))
        conn.commit()
        assert not _checks(pl.craft_checks(_ctx(conn, 12, _PLAIN)), "promise_short_overdue")

    def test_short_line_english_uses_word_limit(self, conn):
        _fs(conn, "fs_s", "The Broken Sword", planted=1, mentioned=10, line_type="short")
        issues = pl.craft_checks(_ctx(conn, 12, "Mara slept.", language="en-US"))
        overdue = _checks(issues, "promise_short_overdue")
        assert len(overdue) == 1 and "22,000 words" in overdue[0]["detail"]
        assert not _CJK.search(overdue[0]["detail"])

    def test_opening_requires_long_and_short(self, conn):
        _fs(conn, "fs_long", "复仇", planted=1, mentioned=2)
        issues = pl.craft_checks(_ctx(conn, 3, _PLAIN))
        opening = _checks(issues, "promise_opening")
        assert len(opening) == 1 and "短线" in opening[0]["detail"]
        assert not _checks(pl.craft_checks(_ctx(conn, 2, _PLAIN)), "promise_opening")
        _fs(conn, "fs_short", "今晚的约战", planted=2, line_type="short")
        assert not _checks(pl.craft_checks(_ctx(conn, 3, _PLAIN)), "promise_opening")

    def test_opening_counts_planned_plants_and_closed_rows(self, conn):
        _fs(conn, "fs_long", "复仇", planted=1, mentioned=2)
        actions = [{"label": "藏经阁的秘密", "action": "plant"}]
        assert not _checks(pl.craft_checks(_ctx(conn, 3, _PLAIN, actions=actions)),
                           "promise_opening")
        _fs(conn, "fs_paid", "小比", planted=1, state="paid_off", line_type="short",
            paid_off=2)
        assert not _checks(pl.craft_checks(_ctx(conn, 3, _PLAIN)), "promise_opening")

    def test_opening_planned_plant_covers_only_one_gap(self, conn):
        actions = [{"label": "藏经阁的秘密", "action": "plant"}]
        opening = _checks(pl.craft_checks(_ctx(conn, 3, _PLAIN, actions=actions)),
                          "promise_opening")
        assert len(opening) == 1
        assert "只能补上其中一类" in opening[0]["detail"]
        assert "再埋下 1 条" in opening[0]["fix"]
        en = _checks(pl.craft_checks(_ctx(conn, 3, "Mara slept.", language="en-US",
                                          actions=actions)), "promise_opening")
        assert "cover only one of them" in en[0]["detail"]
        assert not _CJK.search(en[0]["detail"] + en[0]["fix"])
        both = actions + [{"label": "今晚的约战", "action": "plant"}]
        assert not _checks(pl.craft_checks(_ctx(conn, 3, _PLAIN, actions=both)),
                           "promise_opening")

    def test_opening_english(self, conn):
        _fs(conn, "fs_short", "Tonight's duel", planted=1, line_type="short")
        opening = _checks(pl.craft_checks(_ctx(conn, 3, "Mara slept.", language="en-US")),
                          "promise_opening")
        assert len(opening) == 1 and "missing: long line" in opening[0]["detail"]

    def test_contract_missing_action(self, conn):
        _fs(conn, "fs_old", "血仇", planted=1, mentioned=10)
        actions = [{"promise_id": "fs_old", "action": "remind", "note": "梦中闪回"}]
        issues = pl.craft_checks(_ctx(conn, 12, _PLAIN, actions=actions))
        missing = _checks(issues, "promise_contract_missing")
        assert len(missing) == 1 and "提醒" in missing[0]["detail"]
        assert "梦中闪回" in missing[0]["fix"]
        ok = pl.craft_checks(_ctx(conn, 12, "梦里，血仇二字烧得他睁开眼。", actions=actions))
        assert ok == []

    def test_label_only_action_without_row(self, conn):
        _fs(conn, "fs_other", "别的承诺", planted=11)
        actions = [{"label": "黑袍人身份", "action": "advance"}]
        issues = pl.craft_checks(_ctx(conn, 12, _PLAIN, actions=actions))
        assert len(_checks(issues, "promise_contract_missing")) == 1
        unknown = [{"promise_id": "fs_missing", "action": "advance"}]
        assert pl.craft_checks(_ctx(conn, 12, _PLAIN, actions=unknown)) == []

    def test_unmapped_variant_label_maps_by_containment(self, conn):
        _fs(conn, "fs_old", "血仇", planted=1)
        _fs(conn, "fs_master", "师父", planted=1, mentioned=10)
        rows = pl.fetch_promises(conn, open_only=False)
        mapped = pl.resolve_action({"label": "血仇未报", "action": "remind"}, rows)
        assert mapped["id"] == "fs_old"
        # 较短一方不足一半：「师父」不吞掉另一条承诺「师父的遗言」
        assert pl.resolve_action({"label": "师父的遗言", "action": "remind"}, rows) is None
        issues = pl.craft_checks(_ctx(conn, 15, _PLAIN, actions=[
            {"label": "血仇未报", "action": "remind"}]))
        assert [i["check"] for i in issues] == ["promise_contract_missing"]   # 不再双报

    def test_planned_remind_suppresses_dormant(self, conn):
        _fs(conn, "fs_old", "血仇", planted=1)
        actions = [{"label": "血仇", "action": "remind"}]
        issues = pl.craft_checks(_ctx(conn, 15, _PLAIN, actions=actions))
        assert [i["check"] for i in issues] == ["promise_contract_missing"]

    def test_thin_setup_before_big_payoff(self, conn):
        _fs(conn, "fs_big", "灭门真相", planted=2, importance=4)
        actions = [{"promise_id": "fs_big", "action": "payoff"}]
        draft = "灭门真相大白于天下。"
        thin = _checks(pl.craft_checks(_ctx(conn, 6, draft, actions=actions)),
                       "promise_thin_setup")
        assert len(thin) == 1 and "扣子没挖够" in thin[0]["detail"]
        # 建议必须可执行：本章内补的提醒不计入铺垫，所以不能让写手"在本章补一笔"了事
        assert "本章内补的提醒不算" in thin[0]["fix"] and "再提醒/推进 1 次" in thin[0]["fix"]
        assert "本章前半段" not in thin[0]["fix"]
        _log(conn, "fs_big", 4)
        assert not _checks(pl.craft_checks(_ctx(conn, 6, draft, actions=actions)),
                           "promise_thin_setup")

    def test_small_short_payoff_needs_no_setup(self, conn):
        _fs(conn, "fs_small", "今晚的约战", planted=5, line_type="short", importance=2)
        actions = [{"promise_id": "fs_small", "action": "payoff"}]
        assert pl.craft_checks(_ctx(conn, 6, "今晚的约战终于开始。", actions=actions)) == []

    def test_payoff_cliff_and_volume_finale(self, conn):
        labels = ["甲之谜", "乙之约", "丙之仇"]
        for i, label in enumerate(labels):
            _fs(conn, f"fs{i}", label, planted=9, line_type="short", importance=2)
        actions = [{"promise_id": f"fs{i}", "action": "payoff"} for i in range(3)]
        draft = "。".join(labels) + "，尽数揭晓。"
        cliff = _checks(pl.craft_checks(_ctx(conn, 10, draft, actions=actions)),
                        "promise_payoff_cliff")
        assert len(cliff) == 1 and "3 条" in cliff[0]["detail"]
        finale = pl.craft_checks(_ctx(conn, 10, draft, actions=actions, volume_end=10))
        assert not _checks(finale, "promise_payoff_cliff")

    def test_nesting_order(self, conn):
        _fs(conn, "fs_parent", "复仇", planted=1, mentioned=8, line_type="short",
            importance=2)
        _fs(conn, "fs_child", "仇人的名字", planted=2, mentioned=8, line_type="short",
            parent="fs_parent")
        actions = [{"promise_id": "fs_parent", "action": "payoff"}]
        draft = "复仇已了。"
        nesting = _checks(pl.craft_checks(_ctx(conn, 9, draft, actions=actions)),
                          "promise_nesting")
        assert len(nesting) == 1 and "仇人的名字" in nesting[0]["detail"]
        both = actions + [{"promise_id": "fs_child", "action": "payoff"}]
        assert not _checks(
            pl.craft_checks(_ctx(conn, 9, "仇人的名字揭晓，复仇已了。", actions=both)),
            "promise_nesting")

    def test_contract_checks_english(self, conn):
        _fs(conn, "fs_p", "The Oath", planted=1, importance=5)
        _fs(conn, "fs_c", "The Traitor", planted=2, mentioned=8, parent="fs_p",
            line_type="short")
        actions = [{"promise_id": "fs_p", "action": "payoff"}]
        issues = pl.craft_checks(_ctx(conn, 9, "Mara kept the oath at last.",
                                      actions=actions, language="en-US"))
        checks = {i["check"] for i in issues}
        assert {"promise_thin_setup", "promise_nesting"} <= checks
        assert not any(_CJK.search(i["detail"] + i["fix"]) for i in issues)
        thin = _checks(issues, "promise_thin_setup")[0]
        assert "earlier in this chapter" not in thin["fix"]
        assert "1 more reminder(s)" in thin["fix"]

    def test_disabled_config_is_silent(self, conn):
        _fs(conn, "fs_old", "血仇", planted=1)
        assert pl.craft_checks(_ctx(conn, 15, _PLAIN, cfg=PromiseConfig(enabled=False))) == []

    def test_workspace_config_thresholds_apply(self, conn):
        _fs(conn, "fs_old", "血仇", planted=1)
        cfg = PromiseConfig(default_remind_every=10)
        assert pl.craft_checks(_ctx(conn, 15, _PLAIN, cfg=cfg)) == []

    def test_database_without_ledger_tables_never_raises(self):
        c = sqlite3.connect(":memory:")
        try:
            assert pl.craft_checks(_ctx(c, 5, _PLAIN)) == []
            issues = pl.craft_checks(_ctx(c, 5, _PLAIN, actions=[
                {"label": "血仇", "action": "payoff"}]))
            assert [i["check"] for i in issues] == ["promise_contract_missing"]
            assert pl.draft_brief(c, 5, {}, language="zh-CN", cfg=None) == ""
            assert "error" in pl.settle(c, 5, {"draft_text": _PLAIN}, gateway=None, cfg=None)
        finally:
            c.close()

    def test_skill_surfaces_warn_issue(self, conn):
        from novelforge.skills.craft_check_skill import CraftCheckSkill

        _fs(conn, "fs_old", "血仇", planted=1)
        ctx = _ctx(conn, 12, _PLAIN)
        ctx.workspace["beats"] = [
            {"beat_type": "setup", "summary": "开场", "value_axis": "平静→紧张"},
            {"beat_type": "hook", "summary": "悬念", "value_axis": "悬念↑"},
        ]
        result = CraftCheckSkill().run(ctx)
        assert result.ok
        dormant = [i for i in ctx.workspace["craft_issues"] if i["check"] == "promise_dormant"]
        assert dormant and dormant[0]["severity"] == "warn"
        assert dormant[0]["source"] == "craft" and dormant[0]["fix"]


# ── draft brief ──────────────────────────────────────────────────────────────

class TestDraftBrief:
    def test_zh_brief_lists_most_dormant(self, conn):
        _fs(conn, "fs_old", "血仇", desc="灭门真凶仍未现身", planted=1, mentioned=1)
        _fs(conn, "fs_mid", "断剑", desc="断剑的来历", planted=3)
        _fs(conn, "fs_new", "约战", desc="三日后的约战", planted=4)
        _fs(conn, "fs_hidden", "幕后黑手", planted=1, visibility="hidden")
        workspace: dict = {}
        brief = pl.draft_brief(conn, 9, workspace, language="zh-CN", cfg=NovelForgeConfig())
        lines = brief.splitlines()
        assert lines[0] == "## 承诺提醒（本章需自然呼应，不要生硬复述）"
        assert lines[1] == "- 血仇（第1章埋下，已 8 章未提及）：灭门真凶仍未现身"
        assert lines[2].startswith("- 断剑（第3章埋下，已 6 章未提及）")
        assert len(lines) == 3                     # inject_dormant_limit=2
        assert "幕后黑手" not in brief
        assert workspace["promise_config"]["inject_dormant_limit"] == 2
        assert not any(marker in brief for marker in _ROUTING_MARKERS)

    def test_en_brief(self, conn):
        _fs(conn, "fs_en", "The Blood Debt", desc="who ordered the massacre", planted=1)
        brief = pl.draft_brief(conn, 9, {}, language="en-US", cfg=None)
        assert brief.splitlines() == [
            "## Promise reminders (weave in naturally, do not recap)",
            "- The Blood Debt (planted in chapter 1, untouched for 8 chapters): "
            "who ordered the massacre",
        ]
        assert not _CJK.search(brief)
        assert not any(marker in brief for marker in _ROUTING_MARKERS)

    def test_planned_actions_carry_ledger_context(self, conn):
        _fs(conn, "fs_parent", "复仇", desc="血债血偿", planted=7, importance=5)
        _fs(conn, "fs_child", "仇人的名字", planted=8, parent="fs_parent")
        _fs(conn, "fs_plain", "新的约定", planted=8)
        workspace = {"plan_context": PlanContext(target_chapter=9, current_contract={
            "promise_actions": [
                {"promise_id": "fs_parent", "action": "payoff", "note": "当众揭晓"},
                {"label": "不存在的承诺", "action": "remind"},
            ]})}
        brief = pl.draft_brief(conn, 9, workspace, language="zh-CN", cfg=None)
        lines = brief.splitlines()
        assert len(lines) == 2
        assert lines[1].startswith("- 本章计划兑现：复仇（第7章埋下）：血债血偿 — 当众揭晓")
        assert "先收束内层承诺「仇人的名字」" in lines[1]
        assert "铺垫尚浅（仅触及 1 次）" in lines[1]
        assert "不存在的承诺" not in brief        # 未映射到账本的操作契约块已渲染，不重复
        en = pl.draft_brief(conn, 9, workspace, language="en-US", cfg=None).splitlines()
        assert en[1].startswith("- Planned payoff: 复仇 (planted in chapter 7): 血债血偿")
        assert 'close the inner promise(s) "仇人的名字" first' in en[1]

    def test_overdue_short_line_is_announced(self, conn):
        # 第11章刚推进过（不在提醒周期内），但已超出短线预算：写手必须事先知道
        _fs(conn, "fs_s", "断剑之谜", desc="断剑从何而来", planted=1, advanced=11,
            line_type="short")
        brief = pl.draft_brief(conn, 12, {}, language="zh-CN", cfg=None)
        assert brief.splitlines()[1] == (
            "- 断剑之谜（短线，第1章埋下，约 33,000 字仍未收束，超出 30,000 字预算）"
            "：断剑从何而来；本章推进或揭开它")
        assert not any(marker in brief for marker in _ROUTING_MARKERS)
        # 告警口径不变（自埋下起计），写手照提示呼应后静默
        assert _checks(pl.craft_checks(_ctx(conn, 12, _PLAIN)), "promise_short_overdue")
        assert pl.craft_checks(_ctx(conn, 12, "断剑之谜终于有了线索。")) == []
        en = pl.draft_brief(conn, 12, {}, language="en-US", cfg=None).splitlines()
        assert en[1] == ("- 断剑之谜 (short line, planted in chapter 1, still open after "
                         "about 22,000 words, over the 20,000-word budget): 断剑从何而来; "
                         "advance or resolve it this chapter")

    def test_due_and_overdue_short_line_listed_once(self, conn):
        _fs(conn, "fs_s", "断剑之谜", desc="断剑从何而来", planted=1, line_type="short")
        lines = pl.draft_brief(conn, 12, {}, language="zh-CN", cfg=None).splitlines()
        assert lines[1:] == [
            "- 断剑之谜（第1章埋下，已 11 章未提及）：断剑从何而来"
            "；短线已超出 30,000 字预算（约 33,000 字），本章推进或揭开它"]

    def test_planned_overdue_short_line_keeps_planner_note(self, conn):
        _fs(conn, "fs_s", "断剑之谜", desc="断剑从何而来", planted=1, advanced=11,
            line_type="short")
        workspace = {"plan_context": PlanContext(target_chapter=12, current_contract={
            "promise_actions": [{"promise_id": "fs_s", "action": "advance",
                                 "note": "铁匠认出剑纹"}]})}
        lines = pl.draft_brief(conn, 12, workspace, language="zh-CN", cfg=None).splitlines()
        assert lines[1:] == [
            "- 本章计划推进：断剑之谜（第1章埋下）：断剑从何而来 — 铁匠认出剑纹"
            "；短线已超出 30,000 字预算（约 33,000 字），本章推进或揭开它"]

    def test_empty_when_nothing_due_or_disabled(self, conn):
        _fs(conn, "fs_recent", "断剑", planted=6)
        assert pl.draft_brief(conn, 9, {}, language="zh-CN", cfg=None) == ""
        _fs(conn, "fs_old", "血仇", planted=1)
        cfg = NovelForgeConfig()
        cfg.promises.enabled = False
        assert pl.draft_brief(conn, 9, {}, language="zh-CN", cfg=cfg) == ""
        assert pl.draft_brief(None, 9, {}, language="zh-CN", cfg=None) == ""


# ── settle ───────────────────────────────────────────────────────────────────

def _settle_ws(chapter, draft, actions, volume_end=None):
    return {
        "draft_text": draft,
        "plan_context": PlanContext(
            target_chapter=chapter,
            current_contract={"chapter": chapter, "promise_actions": actions},
            current_volume=({"end_chapter": volume_end} if volume_end else None),
        ),
    }


class TestSettle:
    def test_remind_applied_idempotently(self, conn):
        _fs(conn, "fs_old", "血仇", planted=1, mentioned=1)
        ws = _settle_ws(9, "夜深了，他又想起血仇。", [{"label": "血仇", "action": "remind"}])
        first = pl.settle(conn, 9, ws, gateway=None, cfg=NovelForgeConfig())
        conn.commit()
        second = pl.settle(conn, 9, ws, gateway=None, cfg=NovelForgeConfig())
        conn.commit()
        assert first["reminded"] == [{"id": "fs_old", "label": "血仇"}]
        assert second["reminded"] == []
        rows = conn.execute("SELECT action, evidence FROM foreshadow_log"
                            " WHERE foreshadow_id='fs_old' AND chapter=9").fetchall()
        assert [r["action"] for r in rows] == ["mention"]
        assert "血仇" in rows[0]["evidence"]
        last = conn.execute("SELECT last_mentioned_chapter FROM foreshadow"
                            " WHERE id='fs_old'").fetchone()[0]
        assert last == 9
        assert set(first) >= {"reminded", "violations", "payoffs", "dormant"}

    def test_remind_respects_existing_touch_and_absent_label(self, conn):
        _fs(conn, "fs_a", "血仇", planted=1, mentioned=12)
        _log(conn, "fs_a", 9, "advance")             # LLM 结算已记录本章推进
        _fs(conn, "fs_b", "断剑", planted=1)
        ws = _settle_ws(9, "他想起血仇。", [
            {"promise_id": "fs_a", "action": "remind"},
            {"promise_id": "fs_b", "action": "remind"},   # 正文没提 → 不记
        ])
        report = pl.settle(conn, 9, ws, gateway=None, cfg=None)
        assert report["reminded"] == []
        n = conn.execute("SELECT COUNT(*) FROM foreshadow_log WHERE chapter=9").fetchone()[0]
        assert n == 1
        # max(existing, chapter)：不把更晚的提及章回拨
        assert conn.execute("SELECT last_mentioned_chapter FROM foreshadow"
                            " WHERE id='fs_a'").fetchone()[0] == 12

    def test_remind_needs_exact_hit_before_writing_ledger(self, conn):
        _fs(conn, "fs_fuzzy", "灭门真凶", desc="灭门真凶", planted=1)
        _fs(conn, "fs_name", "仇人的名字", desc="仇人的名字", planted=1)
        actions = [{"promise_id": "fs_fuzzy", "action": "remind"},
                   {"promise_id": "fs_name", "action": "remind"}]
        draft = "那灭门的真凶究竟是谁？他的名字叫张三。"
        # 检查侧：模糊命中「灭门真凶」算已呼应；「仇人的名字」不被虚字二元组误判
        missing = _checks(pl.craft_checks(_ctx(conn, 9, draft, actions=actions)),
                          "promise_contract_missing")
        assert ["仇人的名字" in i["detail"] for i in missing] == [True]
        # 写账本侧：只认精确命中，两条都不落 mention
        report = pl.settle(conn, 9, _settle_ws(9, draft, actions), gateway=None, cfg=None)
        assert report["reminded"] == [] and "error" not in report
        assert conn.execute("SELECT COUNT(*) FROM foreshadow_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM foreshadow"
                            " WHERE last_mentioned_chapter IS NOT NULL").fetchone()[0] == 0

    def test_settle_leaves_commit_to_orchestrator(self, tmp_path):
        from novelforge.db.connection import init_db_from_conn

        path = tmp_path / "ledger.db"
        main = sqlite3.connect(path)
        main.row_factory = sqlite3.Row
        init_db_from_conn(main)
        _fs(main, "fs_old", "血仇", planted=1)
        ws = _settle_ws(9, "他又想起血仇。", [{"label": "血仇", "action": "remind"}])
        other = sqlite3.connect(path)
        try:
            assert not main.in_transaction
            report = pl.settle(main, 9, ws, gateway=None, cfg=None)
            assert report["reminded"] == [{"id": "fs_old", "label": "血仇"}]
            # 没有自行提交：写入留在事务里，别的连接还看不到
            assert main.in_transaction
            count = "SELECT COUNT(*) FROM foreshadow_log WHERE chapter=9"
            assert other.execute(count).fetchone()[0] == 0
            main.commit()
            assert other.execute(count).fetchone()[0] == 1
            # 嵌套在调用方事务里：SAVEPOINT 隔离、不提交，随调用方回滚
            main.execute("UPDATE foreshadow SET importance=5 WHERE id='fs_old'")
            ws10 = _settle_ws(10, "血仇未报。", [{"label": "血仇", "action": "remind"}])
            assert pl.settle(main, 10, ws10, gateway=None, cfg=None)["reminded"]
            assert main.in_transaction
            main.rollback()
            assert main.execute("SELECT COUNT(*) FROM foreshadow_log"
                                " WHERE chapter=10").fetchone()[0] == 0
            assert main.execute("SELECT importance FROM foreshadow"
                                " WHERE id='fs_old'").fetchone()[0] == 3
        finally:
            other.close()
            main.close()

    def test_violations_and_counts(self, conn):
        _fs(conn, "fs_parent", "复仇", planted=1, state="paid_off", paid_off=12,
            importance=5)
        _fs(conn, "fs_child", "仇人的名字", planted=2, mentioned=10, parent="fs_parent")
        _fs(conn, "fs_x", "甲", planted=10, state="paid_off", paid_off=12, line_type="short")
        _fs(conn, "fs_y", "乙", planted=10, state="paid_off", paid_off=12, line_type="short")
        _fs(conn, "fs_dormant", "旧约", planted=1)
        report = pl.settle(conn, 12, _settle_ws(12, "复仇已了。", []), gateway=None, cfg=None)
        assert report["payoffs"] == 3
        kinds = {v["type"]: v for v in report["violations"]}
        assert kinds["nesting"]["open_children"] == [{"id": "fs_child", "label": "仇人的名字"}]
        assert kinds["payoff_cliff"]["count"] == 3
        assert kinds["thin_setup"]["id"] == "fs_parent" and kinds["thin_setup"]["touches"] == 1
        assert len(report["violations"]) == 3            # 小短线不算大爽点
        assert report["dormant"] == 1                    # 旧约：到第 13 章已 12 章未触及
        finale = pl.settle(conn, 12, _settle_ws(12, "复仇已了。", [], volume_end=12),
                           gateway=None, cfg=None)
        assert "payoff_cliff" not in {v["type"] for v in finale["violations"]}

    def test_disabled_and_errors_never_raise(self, conn):
        cfg = NovelForgeConfig()
        cfg.promises.enabled = False
        assert pl.settle(conn, 3, {}, gateway=None, cfg=cfg)["skipped"] == "disabled"
        broken = sqlite3.connect(":memory:")
        broken.close()
        report = pl.settle(broken, 3, _settle_ws(3, "血仇", [{"label": "血仇",
                                                            "action": "remind"}]),
                           gateway=None, cfg=None)
        assert "error" in report


# ── 管线集成 ─────────────────────────────────────────────────────────────────

_BEATS_JSON = json.dumps([
    {"beat_type": "setup", "summary": "开场", "value_axis": "平静→波澜"},
    {"beat_type": "turn", "summary": "转折", "value_axis": "守→攻"},
    {"beat_type": "hook", "summary": "章末悬念", "value_axis": "悬念↑"},
], ensure_ascii=False)


def _draft_response(body: str) -> str:
    return f"```draft\n{body}\n```\n```proposals\n[]\n```"


def _factory(draft_body):
    def factory(messages, model="", temperature=1.0):
        user = str(messages[-1].content) if messages else ""
        if "规划任务" in user:
            return _BEATS_JSON
        if "本章任务" in user:
            return _draft_response(draft_body)
        if "未解伏笔" in user:
            return '{"settlements": [], "new_hooks": []}'
        return "[]"
    return factory


def _build_orch(project, factory, *, settle=True):
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
    cfg = NovelForgeConfig(project_id=project)
    cfg.provider.provider = "fake"
    cfg.recall.enable_summaries = False
    cfg.settle.enabled = settle
    return Orchestrator(gw, reg, cfg), fake


class TestPipeline:
    def test_brief_lands_in_dynamic_section(self, client, project):
        conn = _open_conn(project)
        try:
            conn.execute(
                "INSERT INTO foreshadow(id,label,description,state,planted_chapter,"
                "last_mentioned_chapter) VALUES('fs_old','血仇','灭门真凶','planted',1,1)")
            conn.commit()
            orch, fake = _build_orch(project, _factory("平稳正文。" * 200))
            outcome = orch.generate_chapter(9, conn, chapter_goal="测试")
            assert outcome.ok
            draft_call = next(c for c in fake.calls
                              if "本章任务" in str(c["messages"][-1].content))
            user = str(draft_call["messages"][-1].content)
            prefix = draft_call["cache_hint"].user_prefix_chars
            assert "血仇" in user and "血仇" not in user[:prefix]
            assert prefix < user.index("## 承诺提醒") < user.index("## 本章任务")
            planner_call = next(c for c in fake.calls
                                if "规划任务" in str(c["messages"][-1].content))
            planner_user = str(planner_call["messages"][-1].content)
            assert planner_user.index("## 承诺提醒") < planner_user.index("## 规划任务")
            # 结算报告落 detail_json，且该步不降级
            detail = json.loads(conn.execute(
                "SELECT detail_json FROM pipeline_run WHERE run_id=?",
                (outcome.run_id,)).fetchone()["detail_json"])
            report = detail["extensions"]["promise_ledger"]
            assert report["reminded"] == [] and report["payoffs"] == 0
            assert "ext_promise_ledger" not in (detail.get("failed_steps") or [])
            # 告警而非阻塞：本章未提血仇 → 第 9 章尚未休眠（8 ≤ 10），无 promise 告警
            assert not [i for i in outcome.issues or []
                        if str(i.get("check", "")).startswith("promise_")]
        finally:
            conn.close()

    def test_contract_remind_settles_to_ledger(self, client, project, monkeypatch):
        import novelforge.control_plane.orchestrator as orch_mod

        def fake_context(conn, chapter, lookahead_chapters=5):
            return PlanContext(target_chapter=chapter, current_contract={
                "chapter": chapter,
                "promise_actions": [{"promise_id": "fs_old", "action": "remind"}],
            })

        monkeypatch.setattr(orch_mod, "gather_plan_context", fake_context)
        conn = _open_conn(project)
        try:
            conn.execute(
                "INSERT INTO foreshadow(id,label,description,state,planted_chapter)"
                " VALUES('fs_old','血仇','灭门真凶','planted',1)")
            conn.commit()
            body = "风雪夜里，他又想起了血仇。" + "平稳正文。" * 150
            orch, fake = _build_orch(project, _factory(body))
            outcome = orch.generate_chapter(4, conn, chapter_goal="测试")
            assert outcome.ok and not outcome.state_degraded
            draft_user = next(str(c["messages"][-1].content) for c in fake.calls
                              if "本章任务" in str(c["messages"][-1].content))
            assert "本章计划提醒：血仇（第1章埋下）" in draft_user
            # 顺序：契约块 < 承诺提醒 < 本章任务
            assert (draft_user.index("## 本章硬契约") < draft_user.index("## 承诺提醒")
                    < draft_user.index("## 本章任务"))
            row = conn.execute("SELECT last_mentioned_chapter FROM foreshadow"
                               " WHERE id='fs_old'").fetchone()
            assert row["last_mentioned_chapter"] == 4
            logs = conn.execute("SELECT action FROM foreshadow_log"
                                " WHERE foreshadow_id='fs_old' AND chapter=4").fetchall()
            assert [r["action"] for r in logs] == ["mention"]
        finally:
            conn.close()


# ── API ──────────────────────────────────────────────────────────────────────

class TestForeshadowApi:
    def test_create_and_response_fields(self, client, project):
        r = client.post(f"/v1/{project}/foreshadow", json={
            "label": "血仇", "description": "灭门真凶", "planted_chapter": 1})
        assert r.status_code == 201
        body = r.json()
        assert body["line_type"] == "long" and body["visibility"] == "reader"
        assert body["promise_kind"] is None and body["parent_id"] is None
        child = client.post(f"/v1/{project}/foreshadow", json={
            "label": "仇人的名字", "description": "黑袍人是谁", "planted_chapter": 2,
            "promise_kind": "MICE Inquiry", "line_type": "short", "visibility": "hidden",
            "parent_id": body["id"], "remind_every": 3}).json()
        assert child["promise_kind"] == "mice_inquiry"
        assert (child["line_type"], child["visibility"]) == ("short", "hidden")
        assert child["parent_id"] == body["id"] and child["remind_every"] == 3
        other = client.post(f"/v1/{project}/foreshadow", json={
            "label": "预言", "description": "d", "planted_chapter": 1,
            "promise_kind": "prophecy"}).json()
        assert other["promise_kind"] == "prophecy"       # 未知类型保留
        listed = client.get(f"/v1/{project}/foreshadow").json()
        assert {r["line_type"] for r in listed} == {"long", "short"}

    @pytest.mark.parametrize("payload", [
        {"line_type": "epic"},
        {"visibility": "public"},
        {"remind_every": 0},
        {"parent_id": "fs_missing"},
    ])
    def test_create_validation(self, client, project, payload):
        r = client.post(f"/v1/{project}/foreshadow", json={
            "label": "x", "description": "d", "planted_chapter": 1, **payload})
        assert r.status_code == 422

    def test_update_fields_cycles_and_clearing(self, client, project):
        def make(label):
            return client.post(f"/v1/{project}/foreshadow", json={
                "label": label, "description": "d", "planted_chapter": 1,
                "due_chapter": 9}).json()["id"]

        a, b, c = make("甲"), make("乙"), make("丙")
        assert client.patch(f"/v1/{project}/foreshadow/{a}",
                            json={"parent_id": a}).status_code == 422
        assert client.patch(f"/v1/{project}/foreshadow/{b}",
                            json={"parent_id": a}).status_code == 200
        assert client.patch(f"/v1/{project}/foreshadow/{c}",
                            json={"parent_id": b}).status_code == 200
        r = client.patch(f"/v1/{project}/foreshadow/{a}", json={"parent_id": c})
        assert r.status_code == 422 and "环" in r.json()["detail"]
        assert client.patch(f"/v1/{project}/foreshadow/{a}",
                            json={"parent_id": "nope"}).status_code == 422
        r = client.patch(f"/v1/{project}/foreshadow/{c}", json={
            "line_type": "micro", "visibility": "hidden", "remind_every": 4,
            "promise_kind": "Vow"})
        body = r.json()
        assert (body["line_type"], body["visibility"], body["remind_every"],
                body["promise_kind"]) == ("micro", "hidden", 4, "vow")
        # 显式 null 清空承诺字段；旧字段 null 仍表示"不变"
        body = client.patch(f"/v1/{project}/foreshadow/{c}", json={
            "parent_id": None, "remind_every": None, "promise_kind": None,
            "due_chapter": None, "line_type": None}).json()
        assert body["parent_id"] is None and body["remind_every"] is None
        assert body["promise_kind"] is None
        assert body["due_chapter"] == 9 and body["line_type"] == "micro"
        assert client.patch(f"/v1/{project}/foreshadow/{c}",
                            json={"line_type": "epic"}).status_code == 422

    def test_delete_parent_detaches_children(self, client, project):
        parent = client.post(f"/v1/{project}/foreshadow", json={
            "label": "外层", "description": "d", "planted_chapter": 1}).json()["id"]
        child = client.post(f"/v1/{project}/foreshadow", json={
            "label": "内层", "description": "d", "planted_chapter": 1,
            "parent_id": parent}).json()["id"]
        assert client.delete(f"/v1/{project}/foreshadow/{parent}").status_code == 204
        assert client.get(f"/v1/{project}/foreshadow/{child}").json()["parent_id"] is None

    def test_dormant_endpoint_and_health(self, client, project):
        conn = _open_conn(project)
        try:
            conn.execute("INSERT INTO pipeline_run(run_id, chapter, project_id, status)"
                         " VALUES('r1', 14, ?, 'completed')", (project,))
            _fs(conn, "fs_long", "血仇", planted=1, mentioned=2)
            _fs(conn, "fs_short", "断剑", planted=9, line_type="short")
            _fs(conn, "fs_hidden", "幕后", planted=1, visibility="hidden")
            _fs(conn, "fs_fresh", "约战", planted=12)
            conn.commit()
        finally:
            conn.close()
        r = client.get(f"/v1/{project}/foreshadow/dormant")
        assert r.status_code == 200, r.text
        rows = r.json()
        assert [x["id"] for x in rows] == ["fs_short", "fs_long"]   # 6/2 > 13/5
        assert rows[1]["chapters_since_touch"] == 13
        assert rows[1]["last_touch_chapter"] == 2
        assert rows[0]["effective_remind_every"] == 2 and rows[0]["dormant_after"] == 4
        assert [x["id"] for x in client.get(
            f"/v1/{project}/foreshadow/dormant?as_of_chapter=13").json()] == ["fs_long"]
        assert client.get(
            f"/v1/{project}/foreshadow/dormant?as_of_chapter=0").status_code == 422
        health = client.get(f"/v1/{project}/foreshadow/health").json()
        assert health["dormant_count"] == 2 and health["open_count"] == 4


# ── foreshadow_settle（结算员输入）────────────────────────────────────────────

def _settle_gw(response: dict):
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway

    fake = FakeProvider(factory=lambda messages, model="", temperature=1.0:
                        json.dumps(response, ensure_ascii=False))
    return LLMGateway(fake, BudgetLedger(max_tokens=1_000_000, max_usd=10.0)), fake


class TestForeshadowSettleLedger:
    def test_prompt_carries_line_visibility_and_parent(self, conn):
        from novelforge.craft.foreshadow_settle import settle_foreshadow

        _fs(conn, "fs_p", "复仇", planted=1)
        _fs(conn, "fs_c", "仇人的名字", planted=2, line_type="short", visibility="hidden",
            parent="fs_p")
        gw, fake = _settle_gw({"settlements": [], "new_hooks": []})
        settle_foreshadow(gw, "fast", conn, 5, "正文。")
        assert len(fake.calls) == 1
        user = str(fake.calls[0]["messages"][-1].content)
        assert user.startswith("## 未解伏笔列表\n")
        assert "label=复仇 描述=复仇［长线·读者可见］" in user
        assert "label=仇人的名字 描述=仇人的名字［短线·仅作者知］（父承诺：复仇）" in user

    def test_dormant_reader_lines_not_starved(self, conn):
        from novelforge.craft.foreshadow_settle import _evaluated_open_rows

        for i in range(22):     # 22 条较早的暗线，按旧口径会占满 LIMIT 20
            _fs(conn, f"fs_h{i:02d}", f"暗线{i}", planted=1 + i, visibility="hidden")
        _fs(conn, "fs_reader", "读者长线", planted=30)
        _fs(conn, "fs_named", "黑袍人身份", planted=40, visibility="hidden",
            mentioned=41)
        rows = _evaluated_open_rows(conn, "雪夜，黑袍人身份成谜。")
        ids = [r["id"] for r in rows]
        assert len(ids) == 20
        assert ids[0] == "fs_named"          # 正文点名的承诺优先
        assert ids[1] == "fs_reader"         # 读者可见优先于暗线
        assert ids[2:] == [f"fs_h{i:02d}" for i in range(18)]   # 余下按最久未触及

    def test_new_hooks_default_short_reader(self, conn):
        from novelforge.craft.foreshadow_settle import settle_foreshadow

        gw, _ = _settle_gw({"settlements": [], "new_hooks": [
            {"label": "黑袍人身份", "description": "雪夜出现的黑袍人到底是谁"}]})
        report = settle_foreshadow(gw, "fast", conn, 5, "正文。")
        assert report["new_created"] == ["黑袍人身份"]
        row = conn.execute("SELECT line_type, visibility, origin FROM foreshadow"
                           " WHERE label='黑袍人身份'").fetchone()
        assert (row["line_type"], row["visibility"], row["origin"]) == (
            "short", "reader", "settle")
