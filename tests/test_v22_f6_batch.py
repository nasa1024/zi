"""v22/F6 多章连写：容量门控、连写输出切分、逐章校验提交、丢弃重写、API/SSE、autopilot。

全部 FakeProvider（LongFake 声明 long_output），无网络。
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from novelforge.config import NovelForgeConfig
from novelforge.control_plane.budget import BudgetLedger
from novelforge.control_plane.llm.fake_provider import FakeProvider
from novelforge.control_plane.llm.gateway import LLMGateway
from novelforge.control_plane.llm.provider import CapabilitySet
from novelforge.control_plane.orchestrator import Orchestrator
from novelforge.control_plane.skill_registry import SkillRegistry
from novelforge.skills import register_default_skills


# ── 公共构件 ───────────────────────────────────────────────────────────────────

class LongFake(FakeProvider):
    """声明 long_output 的 FakeProvider（Gemini 4 long decode 的离线替身）。"""

    max_out = 1_048_576

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.max_tokens_seen: list[int] = []

    def generate(self, messages, *, model, max_tokens=4096, **kw):
        self.max_tokens_seen.append(max_tokens)
        return super().generate(messages, model=model, max_tokens=max_tokens, **kw)

    def capabilities(self, model):
        return CapabilitySet(max_tokens_out=self.max_out, context_window=1_048_576,
                             long_output=True)


BEATS = [
    {"beat_type": "setup", "summary": "开场", "value_axis": "平静→波澜"},
    {"beat_type": "turn", "summary": "转折", "value_axis": "守→攻"},
    {"beat_type": "hook", "summary": "章末悬念", "value_axis": "悬念↑"},
]
BEATS_JSON = json.dumps(BEATS, ensure_ascii=False)
_SECTION_ZH = re.compile(r"## 连写第 \d+/\d+ 章 · 第 (\d+) 章")
_PLANNER_CH = re.compile(r"当前章节：第 (\d+) 章")
_DRAFT_CH = re.compile(r"第 (\d+) 章\n目标字数")


def chapter_body(chapter: int, n: int = 120) -> str:
    return f"第{chapter}章正文平稳推进。" * n          # 1200+ 字，过中文硬下限 1000


def batch_output(chapters, *, short=()) -> str:
    parts = []
    for c in chapters:
        body = "太短了。" if c in short else chapter_body(c)
        parts.append(f"【第{c}章·开始】\n{body}\n【第{c}章·结束】")
    return "\n\n".join(parts)


def users(fake) -> list[str]:
    return [str(c["messages"][-1].content) for c in fake.calls]


class Script:
    """按路由标记应答的 FakeProvider factory；可按调用序号定制连写/规划/修订输出。"""

    def __init__(self, *, batch=None, planner=None, revise=None, draft=None, on_batch=None):
        self.batch = batch            # (call_no, chapters) -> str
        self.planner = planner        # (chapter, call_no_for_chapter) -> str
        self.revise = revise          # (call_no) -> str（可 raise）
        self.draft = draft            # (chapter) -> str
        self.on_batch = on_batch      # (call_no, chapters) -> None
        self.batch_calls: list[list[int]] = []
        self.planner_calls: dict[int, int] = {}
        self.revise_calls = 0

    def __call__(self, messages, model="", temperature=1.0):
        user = str(messages[-1].content) if messages else ""
        if "## 连写任务" in user:
            chapters = [int(x) for x in _SECTION_ZH.findall(user)]
            self.batch_calls.append(chapters)
            n = len(self.batch_calls)
            if self.on_batch:
                self.on_batch(n, chapters)
            return self.batch(n, chapters) if self.batch else batch_output(chapters)
        if "一致性问题" in user:          # 全文重写（须先于 "草稿：" 判断）
            self.revise_calls += 1
            if self.revise:
                return self.revise(self.revise_calls)
            return "[]"
        if "修订补丁任务" in user:
            return "[]"
        if "规划任务" in user:
            chapter = int(_PLANNER_CH.search(user).group(1))
            self.planner_calls[chapter] = self.planner_calls.get(chapter, 0) + 1
            if self.planner:
                return self.planner(chapter, self.planner_calls[chapter])
            return BEATS_JSON
        if "本章任务" in user:
            chapter = int(_DRAFT_CH.search(user).group(1))
            body = self.draft(chapter) if self.draft else chapter_body(chapter)
            return f"```draft\n{body}\n```"
        if "未解伏笔" in user:
            return '{"settlements": [], "new_hooks": []}'
        return "[]"


def make_factory(db_path, provider, *, project="p", tweak=None, scales=None,
                 batch_ledger_tokens=10_000_000):
    """run_batch 的 make_orchestrator(budget_scale)：每次全新 gateway/ledger。"""

    def make(budget_scale: int = 1) -> Orchestrator:
        if scales is not None:
            scales.append(budget_scale)
        max_tokens = batch_ledger_tokens if budget_scale > 1 else 10_000_000
        gw = LLMGateway(provider, BudgetLedger(max_tokens=max_tokens, max_usd=100.0,
                                               max_revise_rounds=100))
        reg = SkillRegistry()
        register_default_skills(reg)
        cfg = NovelForgeConfig(project_id=project, db_path=str(db_path))
        cfg.provider.provider = "fake"
        cfg.recall.enable_summaries = False
        cfg.settle.enabled = False
        if tweak:
            tweak(cfg)
        return Orchestrator(gw, reg, cfg)

    return make


@pytest.fixture
def db(tmp_path):
    from novelforge.db.connection import init_db
    path = tmp_path / "novel.db"
    conn = init_db(path)
    yield conn, path
    conn.close()


def _seed_entity(conn) -> None:
    conn.execute(
        "INSERT INTO entities(id, canonical_name, entity_type) VALUES('ent_lt', '陆天', 'character')"
    )
    conn.commit()


def _batch_rows(conn, batch_id):
    return [
        (r["chapter"], r["status"], r["reason"])
        for r in conn.execute(
            "SELECT chapter, status, reason FROM draft_batch_chapters WHERE batch_id=? ORDER BY seq",
            (batch_id,),
        ).fetchall()
    ]


def _draft_index(conn):
    return [
        (r["chapter"], r["revision_round"])
        for r in conn.execute(
            "SELECT chapter, revision_round FROM draft_index ORDER BY rowid"
        ).fetchall()
    ]


# ── 1. 容量门控 ───────────────────────────────────────────────────────────────

class TestCapacity:
    def _gw(self, provider):
        return LLMGateway(provider, BudgetLedger(max_tokens=10_000_000, max_usd=100.0))

    def test_capacity_gating(self, db):
        from novelforge.control_plane.batch import batch_capacity
        from novelforge.skills.chapter_batch_draft_skill import per_chapter_output_tokens
        from novelforge.writing_profiles import get_project_writing_profile

        conn, _ = db
        profile = get_project_writing_profile(conn, chapter=1)
        cfg = NovelForgeConfig()
        # FakeProvider：long_output=False → 单章流程
        assert batch_capacity(self._gw(FakeProvider()), cfg, profile) == 1
        # 长输出模型 → 受 max_chapters 封顶
        assert batch_capacity(self._gw(LongFake()), cfg, profile) == cfg.batch.max_chapters
        # 输出预算只够 3 章 → 3
        small = LongFake()
        small.max_out = per_chapter_output_tokens(profile, cfg.draft_target_chars) * 3 + 10
        assert batch_capacity(self._gw(small), cfg, profile) == 3
        # 关闭 / gateway 缺失 → 1
        disabled = NovelForgeConfig()
        disabled.batch.enabled = False
        assert batch_capacity(self._gw(LongFake()), disabled, profile) == 1
        assert batch_capacity(None, cfg, profile) == 1
        # 不要求 long_output 时按 token 预算算：8192 // 5824 = 1
        relaxed = NovelForgeConfig()
        relaxed.batch.require_long_output = False
        assert batch_capacity(self._gw(FakeProvider()), relaxed, profile) == 1

    def test_unprobeable_provider_is_single_chapter(self, db):
        from novelforge.control_plane.batch import batch_capacity
        from novelforge.writing_profiles import get_project_writing_profile

        class Broken(FakeProvider):
            def capabilities(self, model):
                raise RuntimeError("no caps")

        conn, _ = db
        profile = get_project_writing_profile(conn, chapter=1)
        assert batch_capacity(self._gw(Broken()), NovelForgeConfig(), profile) == 1

    def test_per_chapter_tokens_follow_profile(self):
        from novelforge.skills.chapter_batch_draft_skill import per_chapter_output_tokens

        zh = {"content_language": "zh-CN", "quality_rules": {}}
        en = {"content_language": "en-US", "quality_rules": {"chapter_word_range": [1500, 2500]}}
        assert per_chapter_output_tokens(zh, 3000) == int(1.6 * 3000) + 1024
        assert per_chapter_output_tokens(en, 3000) == int(2500 * 1.8) + 1024


# ── 2. 连写输出切分（确定性解析）─────────────────────────────────────────────

class TestParser:
    def _parse(self, text, chapters, language="zh-CN", hard_min=20, truncated=False):
        from novelforge.skills.chapter_batch_draft_skill import parse_batch_output
        return parse_batch_output(text, chapters, language=language, hard_min=hard_min,
                                  truncated=truncated)

    def test_markers_split_chapters(self):
        text = (
            "【第3章·开始】\n" + "甲" * 30 + "\n【第3章·结束】\n"
            "【第4章·开始】\n" + "乙" * 30 + "\n【第4章·结束】"
        )
        r = self._parse(text, [3, 4])
        assert r.drafts == {3: "甲" * 30, 4: "乙" * 30}
        assert r.rejects == {}

    def test_missing_final_end_marker_and_fences(self):
        text = (
            "```text\n【第3章·开始】\n" + "甲" * 30 + "\n【第3章·结束】\n\n"
            "  **【第4章 · 开始】**  \n```draft\n" + "乙" * 30 + "\n```\n"
        )
        r = self._parse(text, [3, 4])
        assert r.drafts[3] == "甲" * 30
        assert r.drafts[4] == "乙" * 30       # 无结束标记：截到文末，去掉围栏

    def test_out_of_order_and_stray_chapters(self):
        text = (
            "【第4章·开始】" + "乙" * 30 + "【第4章·结束】"
            "【第9章·开始】" + "丙" * 30 + "【第9章·结束】"
            "【第3章·开始】" + "甲" * 30 + "【第3章·结束】"
        )
        r = self._parse(text, [3, 4])
        assert set(r.drafts) == {3, 4} and 9 not in r.rejects
        assert r.drafts[3] == "甲" * 30

    def test_missing_end_falls_back_to_next_start(self):
        text = "【第3章·开始】" + "甲" * 30 + "\n【第4章·开始】" + "乙" * 30
        r = self._parse(text, [3, 4])
        assert r.drafts == {3: "甲" * 30, 4: "乙" * 30}

    def test_too_short_and_missing_rejected(self):
        text = "【第3章·开始】短【第3章·结束】"
        r = self._parse(text, [3, 4])
        assert r.drafts == {}
        assert r.rejects == {3: "too_short", 4: "missing"}

    def test_truncated_tail_rejected_only_when_output_hit_max_tokens(self):
        text = ("【第3章·开始】" + "甲" * 30 + "【第3章·结束】"
                "【第4章·开始】" + "乙" * 30)               # 第 4 章写到一半被截断
        assert set(self._parse(text, [3, 4]).drafts) == {3, 4}
        r = self._parse(text, [3, 4], truncated=True)
        assert set(r.drafts) == {3} and r.rejects == {4: "truncated"}
        closed = text + "【第4章·结束】"                   # 有结束标记：完整章
        assert set(self._parse(closed, [3, 4], truncated=True).drafts) == {3, 4}

    def test_empty_chapter_rejected(self):
        r = self._parse("【第3章·开始】\n\n【第3章·结束】", [3])
        assert r.rejects == {3: "empty"}

    def test_english_markers_and_language_check(self):
        good = "The lantern swung over the gate while Mara counted the guards. " * 4
        text = (
            "=== CHAPTER 5 START ===\n" + good + "\n=== CHAPTER 5 END ===\n"
            "=== chapter 6 start ===\n" + "这是一段错误语言的正文。" * 10 + "\n=== CHAPTER 6 END ==="
        )
        r = self._parse(text, [5, 6], language="en-US", hard_min=20)
        assert r.drafts == {5: good.strip()}
        assert r.rejects == {6: "language_mismatch"}


# ── 3. 连写 prompt ───────────────────────────────────────────────────────────

_FORBIDDEN_MARKERS = (
    "本章任务", "## Chapter Task", "规划任务", "## Planning Task", "未解伏笔", "### 候选",
    "本章目标：", "Chapter goal: ", "草稿：", "一致性问题", "修订补丁任务", "润色补丁任务",
    "工艺问题", "章正文：", "各章摘要", "章正文如下：", "场景原文",
)


class TestBatchPrompt:
    def _ws(self, stable="## 项目写作配置（锁定）\n缓存标识：zh"):
        return {
            "stable_context": stable,
            "dynamic_context": "当前境界：炼气一层",
            "craft_brief": "## 承诺提醒\n- 血仇需本批内提醒",
            "batch_chapters": [
                {"chapter": 7, "goal": "入门", "beats": BEATS,
                 "contract": "## 本章硬契约\n- 核心目标：入门\n\n### 必须推进\n- 拜师"},
                {"chapter": 8, "goal": "试炼", "beats": [], "contract": ""},
            ],
        }

    def test_zh_prompt_order_markers_and_demoted_contract(self):
        from novelforge.skills.chapter_batch_draft_skill import _SYSTEM, build_batch_prompt

        ws = self._ws()
        profile = {"content_language": "zh-CN", "quality_rules": {}}
        msg, stable = build_batch_prompt(ws, profile, target_chars=3000)
        assert msg.startswith(stable) and stable.startswith("## 项目写作配置（锁定）")
        order = [msg.index(x) for x in (
            "## 世界状态（当前）", "## 承诺提醒", "## 连写第 1/2 章 · 第 7 章",
            "### 本章硬契约", "#### 必须推进", "### 第 7 章 Beat Sheet",
            "## 连写第 2/2 章 · 第 8 章", "## 连写任务",
        )]
        assert order == sorted(order)
        assert "【第7章·开始】" in msg and "章节目标：试炼" in msg and "(无 beats)" in msg
        for marker in _FORBIDDEN_MARKERS:
            assert marker not in msg, marker
        assert "创作助手" not in _SYSTEM and "连写助手" in _SYSTEM

    def test_en_prompt_has_no_chinese_labels(self):
        from novelforge.skills.chapter_batch_draft_skill import _SYSTEM_EN, build_batch_prompt

        ws = {
            "stable_context": "## Locked Writing Profile\nCache identity: en",
            "dynamic_context": "Mara is a novice.",
            "batch_chapters": [
                {"chapter": 5, "goal": "", "beats": [], "contract": "## Chapter contract\n- Goal: x"},
                {"chapter": 6, "goal": "Escape", "beats": [
                    {"beat_type": "hook", "summary": "The gate falls", "value_axis": "safe→trapped",
                     "witnesses": ["Mara"]}], "contract": ""},
            ],
        }
        profile = {"content_language": "en-US",
                   "quality_rules": {"chapter_word_range": [1500, 2500]}}
        msg, _ = build_batch_prompt(ws, profile)
        assert re.search(r"[㐀-鿿]", msg) is None
        assert "## Batch Writing Task" in msg and "=== CHAPTER 5 START ===" in msg
        assert "### Chapter contract" in msg and "(no beats)" in msg
        for marker in _FORBIDDEN_MARKERS:
            assert marker not in msg, marker
        assert "serial drafting assistant" in _SYSTEM_EN

    def test_skill_run_english_project(self, db):
        from novelforge.control_plane.skill_base import SkillContext
        from novelforge.skills.chapter_batch_draft_skill import (
            ChapterBatchDraftSkill,
            per_chapter_output_tokens,
        )
        from novelforge.writing_profiles import (
            get_project_writing_profile,
            initialize_project_writing_profile,
        )

        conn, _ = db
        initialize_project_writing_profile(conn, {
            "content_language": "en-US", "target_market": "global_english",
            "publication_format": "web_serial", "primary_genre": "fantasy",
            "writing_profile_id": "en_progression_serial_v1",
        })
        profile = get_project_writing_profile(conn, chapter=5)
        words = ("Mara crossed the yard and studied the old gate with care. " * 140).strip()

        def factory(messages, model=""):
            return (f"=== CHAPTER 5 START ===\n{words}\n=== CHAPTER 5 END ===\n"
                    "=== CHAPTER 6 START ===\n" + "错误语言。" * 400 + "\n=== CHAPTER 6 END ===")

        fake = LongFake(factory=factory)
        gw = LLMGateway(fake, BudgetLedger(max_tokens=10_000_000, max_usd=100.0))
        stable = "## Locked Writing Profile\nCache identity: en"
        ws = {"writing_profile": profile, "stable_context": stable,
              "batch_chapters": [{"chapter": 5, "goal": "g", "beats": BEATS, "contract": ""},
                                 {"chapter": 6, "goal": "g", "beats": BEATS, "contract": ""}]}
        ctx = SkillContext(project_id="p", target_chapter=5, mode="human_gate", as_of_chapter=4,
                           budget=gw.ledger, llm=gw, conn=conn, workspace=ws,
                           extra={"draft_target_chars": 3000})
        result = ChapterBatchDraftSkill().run(ctx)
        assert result.ok
        assert ws["batch_drafts"] == {5: words}
        assert ws["batch_rejects"] == {6: "language_mismatch"}
        call = fake.calls[0]
        assert call["cache_hint"].user_prefix_chars == len(stable)
        assert "serial drafting assistant" in call["system"]
        assert fake.max_tokens_seen == [2 * per_chapter_output_tokens(profile, 3000)]


# ── 4. 编排器接缝 ─────────────────────────────────────────────────────────────

class TestOrchestratorSeam:
    def test_predrafted_text_skips_plan_and_draft(self, db):
        conn, path = db
        script = Script()
        fake = FakeProvider(factory=script)
        events = []
        outcome = make_factory(path, fake)().generate_chapter(
            1, conn, chapter_goal="入门",
            predrafted_text=chapter_body(1), predrafted_beats=BEATS, batch_id="batch_x",
            progress_cb=lambda stage, status, detail: events.append((stage, detail)),
        )
        assert outcome.ok and outcome.run_id
        assert not any("本章任务" in u or "规划任务" in u for u in users(fake))
        plan = next(d for s, d in events if s == "plan")
        assert plan == {"source": "batch", "beats": 3}
        row = conn.execute(
            "SELECT status, batch_id, detail_json FROM pipeline_run WHERE run_id=?",
            (outcome.run_id,),
        ).fetchone()
        detail = json.loads(row["detail_json"])
        assert row["status"] == "completed" and row["batch_id"] == "batch_x"
        assert detail["selected_by"] == "batch" and detail["batch_id"] == "batch_x"
        assert _draft_index(conn) == [(1, 0)]

    def test_predrafted_text_without_beats_falls_back_to_planner(self, db):
        conn, path = db
        fake = FakeProvider(factory=Script())
        outcome = make_factory(path, fake)().generate_chapter(
            1, conn, chapter_goal="入门", predrafted_text=chapter_body(1),
        )
        assert outcome.ok
        assert any("规划任务" in u for u in users(fake))
        assert not any("本章任务" in u for u in users(fake))

    def test_legacy_call_is_unchanged(self, db):
        conn, path = db
        fake = FakeProvider(factory=Script())
        outcome = make_factory(path, fake)().generate_chapter(1, conn, chapter_goal="入门")
        assert outcome.ok
        assert sum("本章任务" in u for u in users(fake)) == 1
        row = conn.execute("SELECT batch_id, detail_json FROM pipeline_run").fetchone()
        assert row["batch_id"] is None
        assert json.loads(row["detail_json"]).get("selected_by") is None

    def test_unexpected_exception_marks_run_crashed(self, db):
        conn, path = db

        def boom(stage, status, detail):
            if stage == "check":
                raise RuntimeError("callback exploded")

        outcome = make_factory(path, FakeProvider(factory=Script()))().generate_chapter(
            1, conn, chapter_goal="入门", progress_cb=boom,
        )
        assert not outcome.ok and outcome.run_id
        assert "callback exploded" in outcome.error
        row = conn.execute(
            "SELECT status, finished_at, detail_json FROM pipeline_run WHERE run_id=?",
            (outcome.run_id,),
        ).fetchone()
        detail = json.loads(row["detail_json"])
        assert row["status"] == "crashed" and row["finished_at"]
        assert detail["failure_reason"] == "exception"
        assert detail["error_type"] == "RuntimeError"
        assert _draft_index(conn) == []


# ── 5. run_batch 端到端 ───────────────────────────────────────────────────────

class TestRunBatch:
    def test_three_chapter_batch_end_to_end(self, db):
        from novelforge.app.chapter_suggest import next_chapter_no
        from novelforge.control_plane.batch import run_batch

        conn, path = db
        script = Script()
        fake = LongFake(factory=script)
        scales: list[int] = []
        pipeline_only: dict[int, int] = {}
        events: list[dict] = []
        out = run_batch(
            conn, start_chapter=1, count=3,
            make_orchestrator=make_factory(path, fake, scales=scales),
            progress_cb=events.append,
            chapter_cb=lambda ch, oc: pipeline_only.__setitem__(ch, oc.usage_tokens),
        )
        assert (out.mode, out.status, out.error) == ("batch", "completed", None)
        assert script.batch_calls == [[1, 2, 3]]            # ONE 连写调用
        assert 3 in scales                                  # 连写调用走 ×3 预算的 gateway
        assert not any("本章任务" in u for u in users(fake))  # 没有单章起草
        assert not any("创作助手" in (c["system"] or "") for c in fake.calls)

        rows = conn.execute(
            "SELECT run_id, chapter, status, batch_id, tokens_spent, detail_json"
            " FROM pipeline_run ORDER BY chapter"
        ).fetchall()
        assert [r["chapter"] for r in rows] == [1, 2, 3]
        assert {r["batch_id"] for r in rows} == {out.batch_id}
        assert all(r["status"] == "completed" for r in rows)
        assert all(json.loads(r["detail_json"])["selected_by"] == "batch" for r in rows)
        assert _draft_index(conn) == [(1, 0), (2, 0), (3, 0)]
        assert next_chapter_no(conn, "p")[0] == 4

        bch = conn.execute(
            "SELECT chapter, status, run_id FROM draft_batch_chapters WHERE batch_id=? ORDER BY seq",
            (out.batch_id,),
        ).fetchall()
        assert [(r["chapter"], r["status"]) for r in bch] == [
            (1, "accepted"), (2, "accepted"), (3, "accepted")]
        assert [r["run_id"] for r in bch] == [r["run_id"] for r in rows]
        batch_row = conn.execute(
            "SELECT status, origin, tokens_spent FROM draft_batches WHERE id=?", (out.batch_id,),
        ).fetchone()
        assert (batch_row["status"], batch_row["origin"]) == ("completed", "api")
        assert batch_row["tokens_spent"] == out.batch_tokens > 0

        # 连写调用成本平摊进各章 pipeline_run 与 outcome
        assert sum(r["tokens_spent"] for r in rows) == out.usage_tokens
        assert out.usage_tokens == sum(pipeline_only.values()) + out.batch_tokens
        for r in rows:
            assert r["tokens_spent"] > pipeline_only[r["chapter"]]

        kinds = [e["event"] for e in events if e["event"] != "stage"]
        assert kinds == ["batch_start", "batch_draft", "chapter_done", "chapter_done",
                         "chapter_done", "batch_done"]
        stages = [e for e in events if e["event"] == "stage"]
        assert stages and all(e["chapter"] in (1, 2, 3) for e in stages)
        assert [e["chapter"] for e in events if e["event"] == "chapter_done"] == [1, 2, 3]
        assert events[-1]["status"] == "completed" and events[-1]["next_chapter"] == 4

    def test_contract_beats_used_and_explicit_goal(self, db):
        from novelforge.control_plane.batch import run_batch

        conn, path = db
        conn.execute("INSERT INTO chapter_cards(id, chapter, title, goal, status)"
                     " VALUES('card2', 2, '夜探', '潜入藏经阁', 'planned')")
        for seq, beat in enumerate(BEATS, 1):
            conn.execute(
                "INSERT INTO beats(id, chapter, seq, beat_type, summary, value_axis, status)"
                " VALUES(?, 2, ?, ?, ?, ?, 'planned')",
                (f"b2_{seq}", seq, beat["beat_type"], beat["summary"] + "·契约",
                 beat["value_axis"]),
            )
        conn.commit()
        script = Script()
        fake = LongFake(factory=script)
        out = run_batch(conn, start_chapter=1, count=2,
                        make_orchestrator=make_factory(path, fake),
                        chapter_goals={"1": "拜入山门"})
        assert out.status == "completed"
        assert script.planner_calls == {1: 1}               # 第 2 章用已计划节拍，不跑 planner
        prompt = next(u for u in users(fake) if "## 连写任务" in u)
        sec2 = prompt[prompt.index("## 连写第 2/2 章 · 第 2 章"):]
        assert "核心目标：潜入藏经阁" in sec2 and "开场·契约" in sec2
        sec1 = prompt[prompt.index("## 连写第 1/2 章 · 第 1 章"):prompt.index("## 连写第 2/2 章")]
        assert "章节目标：拜入山门" in sec1

    def test_batch_prefix_matches_single_chapter_prefix(self, tmp_path):
        """连写 prompt 的稳定前缀与单章起草 prompt 逐字节一致（前缀缓存可复用）。"""
        from novelforge.control_plane.batch import run_batch
        from novelforge.db.connection import init_db

        base = tmp_path / "base.db"
        conn = init_db(base)
        _seed_entity(conn)
        conn.close()
        single_db, batch_db = tmp_path / "a" / "novel.db", tmp_path / "b" / "novel.db"
        for target in (single_db, batch_db):
            target.parent.mkdir()
            shutil.copy(base, target)

        conn = init_db(single_db)
        single = FakeProvider(factory=Script())
        assert make_factory(single_db, single)().generate_chapter(1, conn, chapter_goal="入门").ok
        conn.close()
        draft_call = next(c for c in single.calls if "本章任务" in str(c["messages"][-1].content))

        conn = init_db(batch_db)
        batched = LongFake(factory=Script())
        run_batch(conn, start_chapter=1, count=2, make_orchestrator=make_factory(batch_db, batched),
                  chapter_goals={"1": "入门"})
        conn.close()
        batch_call = next(c for c in batched.calls if "## 连写任务" in str(c["messages"][-1].content))

        n_single = draft_call["cache_hint"].user_prefix_chars
        n_batch = batch_call["cache_hint"].user_prefix_chars
        prefix = str(draft_call["messages"][-1].content)[:n_single]
        assert n_single == n_batch and "陆天" in prefix
        assert prefix.startswith("## 项目写作配置（锁定）")
        assert str(batch_call["messages"][-1].content)[:n_batch] == prefix

    def test_parse_rejection_discards_tail_and_regenerates(self, db):
        from novelforge.control_plane.batch import run_batch

        conn, path = db
        committed_at_retry: list[int] = []

        def on_batch(n, chapters):
            if n == 2:
                side = sqlite3.connect(str(path))
                committed_at_retry.append(
                    side.execute("SELECT COALESCE(MAX(chapter), 0) FROM draft_index").fetchone()[0])
                side.close()

        script = Script(
            batch=lambda n, chs: batch_output(chs, short={2} if n == 1 else ()),
            on_batch=on_batch,
        )
        events: list[dict] = []
        out = run_batch(conn, start_chapter=1, count=3,
                        make_orchestrator=make_factory(path, LongFake(factory=script)),
                        progress_cb=events.append)
        assert script.batch_calls == [[1, 2, 3], [2, 3]]
        assert committed_at_retry == [1]                    # 重写前只有第 1 章落库
        assert (out.status, out.regenerations, len(out.batch_ids)) == ("completed", 1, 2)
        assert _draft_index(conn) == [(1, 0), (2, 0), (3, 0)]
        first, second = out.batch_ids
        assert _batch_rows(conn, first) == [
            (1, "accepted", None), (2, "rejected", "too_short"), (3, "discarded", "after_rejected:2")]
        assert [r[:2] for r in _batch_rows(conn, second)] == [(2, "accepted"), (3, "accepted")]
        assert out.discarded == [
            {"chapter": 2, "batch_id": first, "reason": "too_short"},
            {"chapter": 3, "batch_id": first, "reason": "after_rejected:2"},
        ]
        statuses = dict(conn.execute("SELECT id, status FROM draft_batches").fetchall())
        assert statuses == {first: "partial", second: "completed"}
        detail = json.loads(conn.execute(
            "SELECT detail_json FROM draft_batches WHERE id=?", (second,)).fetchone()[0])
        assert detail["kind"] == "regenerate" and detail["root_batch_id"] == first
        discarded = next(e for e in events if e["event"] == "batch_discarded")
        regen = next(e for e in events if e["event"] == "batch_regenerate")
        assert (discarded["from_chapter"], discarded["chapters"]) == (2, [2, 3])
        assert (regen["from_chapter"], regen["attempt"], regen["batch_id"]) == (2, 1, second)
        assert events.index(discarded) < events.index(regen)

    def test_failed_chapter_is_rejected_and_rewritten(self, db):
        """第 2 章流水线失败（修订调用异常 → ok=False，run 标 crashed）→ 第 3 章作废，从第 2 章重写。"""
        from novelforge.control_plane.batch import run_batch

        conn, path = db

        def revise(n):
            if n == 1:
                raise RuntimeError("revise exploded")
            return chapter_body(2)

        script = Script(revise=revise)
        # 首个窗口里第 2 章规划失败（含升档重试）→ 无节拍 → beat_contract block → 触发修订
        script.planner = lambda ch, n: "[]" if (ch == 2 and not script.batch_calls) else BEATS_JSON
        out = run_batch(conn, start_chapter=1, count=3,
                        make_orchestrator=make_factory(path, LongFake(factory=script)))
        assert script.batch_calls == [[1, 2, 3], [2, 3]]
        assert (out.status, out.regenerations) == ("completed", 1)
        first, second = out.batch_ids
        failed = [o for o in out.rejected]
        assert len(failed) == 1 and failed[0].chapter == 2 and "revise exploded" in failed[0].error
        crashed = conn.execute(
            "SELECT batch_id, detail_json FROM pipeline_run WHERE status='crashed'").fetchall()
        assert len(crashed) == 1 and crashed[0]["batch_id"] == first
        assert json.loads(crashed[0]["detail_json"])["failure_reason"] == "exception"
        rows = _batch_rows(conn, first)
        assert rows[0][:2] == (1, "accepted")
        assert rows[1][:2] == (2, "rejected") and "revise exploded" in rows[1][2]
        assert rows[2] == (3, "discarded", "after_failed:2")
        assert _draft_index(conn) == [(1, 0), (2, 0), (3, 0)]   # 失败稿从未落库（每章只有首个修订轮）
        assert [o.chapter for o in out.outcomes] == [1, 2, 2, 3]
        assert [s["ok"] for s in out.to_summary()["chapters"]] == [True, False, True, True]

    def test_unresolved_blocks_keep_chapter_and_rewrite_next(self, db):
        from novelforge.control_plane.batch import run_batch

        conn, path = db

        def tweak(cfg):
            cfg.max_revise_loops = 0          # 不修订 → block 残留

        script = Script()
        script.planner = lambda ch, n: "[]" if (ch == 1 and not script.batch_calls) else BEATS_JSON
        out = run_batch(conn, start_chapter=1, count=3,
                        make_orchestrator=make_factory(path, LongFake(factory=script), tweak=tweak))
        assert script.batch_calls == [[1, 2, 3], [2, 3]]
        first = out.batch_ids[0]
        assert _batch_rows(conn, first) == [
            (1, "accepted", None), (2, "discarded", "after_blocked:1"),
            (3, "discarded", "after_blocked:1")]
        assert out.accepted[0].chapter == 1
        assert any(i.get("severity") == "block" for i in out.accepted[0].issues)
        assert (out.status, out.regenerations) == ("completed", 1)
        assert _draft_index(conn) == [(1, 0), (2, 0), (3, 0)]

    def test_max_regenerations_respected(self, db):
        from novelforge.control_plane.batch import run_batch

        conn, path = db

        def tweak(cfg):
            cfg.batch.max_regenerations = 1

        script = Script(batch=lambda n, chs: batch_output(chs, short={2}))
        out = run_batch(conn, start_chapter=1, count=3,
                        make_orchestrator=make_factory(path, LongFake(factory=script), tweak=tweak))
        assert script.batch_calls == [[1, 2, 3], [2, 3]]
        assert (out.status, out.regenerations, out.next_chapter) == ("partial", 1, 2)
        assert "max regenerations" in out.error
        assert [o.chapter for o in out.accepted] == [1]
        assert _draft_index(conn) == [(1, 0)]
        statuses = [r[0] for r in conn.execute(
            "SELECT status FROM draft_batches ORDER BY created_at, rowid").fetchall()]
        assert statuses == ["partial", "failed"]

    def test_batch_call_exception_fails_without_regeneration(self, db):
        from novelforge.control_plane.batch import run_batch

        conn, path = db

        def explode(n, chapters):
            raise RuntimeError("provider exploded")

        events: list[dict] = []
        out = run_batch(conn, start_chapter=1, count=3,
                        make_orchestrator=make_factory(path, LongFake(factory=Script(batch=explode))),
                        progress_cb=events.append)
        assert (out.status, out.error_kind, out.regenerations) == ("failed", "exception", 0)
        assert "provider exploded" in out.error
        row = conn.execute("SELECT status, tokens_spent, detail_json FROM draft_batches").fetchone()
        assert row["status"] == "failed" and "provider exploded" in json.loads(row["detail_json"])["error"]
        draft_ev = next(e for e in events if e["event"] == "batch_draft")
        assert draft_ev["parsed"] == [] and "provider exploded" in draft_ev["error"]
        assert draft_ev["tokens"] == row["tokens_spent"] == out.usage_tokens > 0   # 规划调用已花钱
        assert _draft_index(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM pipeline_run").fetchone()[0] == 0

    def test_batch_budget_trip_reports_circuit_broken(self, db):
        from novelforge.control_plane.batch import run_batch

        conn, path = db
        script = Script()
        out = run_batch(conn, start_chapter=1, count=3,
                        make_orchestrator=make_factory(path, LongFake(factory=script),
                                                       batch_ledger_tokens=500))
        assert (out.status, out.error_kind) == ("failed", "circuit_broken")
        assert script.batch_calls == []
        assert conn.execute("SELECT status FROM draft_batches").fetchone()[0] == "failed"

    def test_capacity_splits_windows_without_regeneration(self, db):
        from novelforge.control_plane.batch import run_batch
        from novelforge.skills.chapter_batch_draft_skill import per_chapter_output_tokens
        from novelforge.writing_profiles import get_project_writing_profile

        conn, path = db
        script = Script()
        fake = LongFake(factory=script)
        fake.max_out = per_chapter_output_tokens(get_project_writing_profile(conn, chapter=1)) * 2
        out = run_batch(conn, start_chapter=1, count=3, make_orchestrator=make_factory(path, fake))
        assert script.batch_calls == [[1, 2], [3]]
        assert (out.status, out.regenerations, out.capacity) == ("completed", 0, 2)
        kinds = [json.loads(r[0])["kind"] for r in conn.execute(
            "SELECT detail_json FROM draft_batches ORDER BY created_at, rowid").fetchall()]
        assert kinds == ["initial", "continue"]

    def test_cancel_between_chapters(self, db):
        from novelforge.control_plane.batch import run_batch

        conn, path = db
        flag = {"cancel": False}
        out = run_batch(
            conn, start_chapter=1, count=3,
            make_orchestrator=make_factory(path, LongFake(factory=Script())),
            cancel_cb=lambda: flag["cancel"],
            chapter_cb=lambda ch, oc: flag.__setitem__("cancel", True),
        )
        assert out.status == "canceled"
        assert [o.chapter for o in out.accepted] == [1]
        assert _batch_rows(conn, out.batch_id) == [
            (1, "accepted", None), (2, "discarded", "canceled"), (3, "discarded", "canceled")]
        assert conn.execute("SELECT status FROM draft_batches").fetchone()[0] == "canceled"
        assert _draft_index(conn) == [(1, 0)]

    def test_sequential_fallback_without_long_output(self, db):
        from novelforge.control_plane.batch import run_batch

        conn, path = db
        fake = FakeProvider(factory=Script())
        events: list[dict] = []
        out = run_batch(conn, start_chapter=1, count=2,
                        make_orchestrator=make_factory(path, fake), progress_cb=events.append)
        assert (out.mode, out.status, out.batch_id) == ("sequential", "completed", None)
        assert sum("本章任务" in u for u in users(fake)) == 2
        assert not any("## 连写任务" in u for u in users(fake))
        assert [r[0] for r in conn.execute("SELECT batch_id FROM pipeline_run").fetchall()] == [
            None, None]
        assert conn.execute("SELECT COUNT(*) FROM draft_batches").fetchone()[0] == 0
        assert events[0] == {"event": "batch_start", "batch_id": None, "mode": "sequential",
                             "chapters": [1, 2], "capacity": 1}
        assert _draft_index(conn) == [(1, 0), (2, 0)]

    def test_sequential_stops_on_failed_chapter(self, db):
        from novelforge.control_plane.batch import run_batch

        conn, path = db
        fake = FakeProvider(factory=Script(draft=lambda ch: "" if ch == 1 else chapter_body(ch)))
        out = run_batch(conn, start_chapter=1, count=3, make_orchestrator=make_factory(path, fake))
        assert (out.mode, out.status) == ("sequential", "failed")
        assert [o.chapter for o in out.outcomes] == [1] and not out.outcomes[0].ok
        assert not any("第 2 章\n目标字数" in u for u in users(fake))


# ── 6. 启动期 sweep ───────────────────────────────────────────────────────────

def _insert_stale_batch(conn, batch_id="batch_stale"):
    conn.execute("INSERT INTO draft_batches(id, start_chapter, end_chapter, status, origin)"
                 " VALUES(?, 1, 2, 'running', 'api')", (batch_id,))
    conn.executemany(
        "INSERT INTO draft_batch_chapters(id, batch_id, chapter, seq, draft_text, status)"
        " VALUES(?, ?, ?, ?, ?, ?)",
        [(f"{batch_id}_1", batch_id, 1, 1, "x", "accepted"),
         (f"{batch_id}_2", batch_id, 2, 2, "y", "pending")],
    )
    conn.execute("INSERT INTO draft_batches(id, start_chapter, end_chapter, status, origin)"
                 " VALUES(?, 3, 3, 'completed', 'api')", (f"{batch_id}_done",))
    conn.commit()


def _assert_swept(conn, batch_id="batch_stale"):
    row = conn.execute("SELECT status, detail_json FROM draft_batches WHERE id=?",
                       (batch_id,)).fetchone()
    assert row["status"] == "failed"
    assert json.loads(row["detail_json"])["failure_reason"] == "interrupted"
    assert _batch_rows(conn, batch_id) == [(1, "accepted", None), (2, "discarded", "interrupted")]
    assert conn.execute("SELECT status FROM draft_batches WHERE id=?",
                        (f"{batch_id}_done",)).fetchone()[0] == "completed"


class TestStartupSweep:
    def test_init_db_sweeps_stale_running_batch(self, tmp_path):
        from novelforge.db.connection import init_db
        from novelforge.db.l0 import sweep_stale_batches

        path = tmp_path / "novel.db"
        conn = init_db(path)
        _insert_stale_batch(conn)
        conn.close()
        conn = init_db(path)          # 已有库 → 迁移 + 启动期 sweep
        try:
            _assert_swept(conn)
            assert sweep_stale_batches(conn) == []      # 幂等
        finally:
            conn.close()

    def test_sweep_tolerates_pre_v22_database(self):
        from novelforge.db.l0 import sweep_stale_batches

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        assert sweep_stale_batches(conn) == []


# ── 7. HTTP API / SSE / autopilot ─────────────────────────────────────────────

@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_DATA", str(tmp_path))
    import novelforge.app.autopilot_manager as ap_mod
    import novelforge.app.deps as deps_mod
    monkeypatch.setattr(deps_mod, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(deps_mod, "_REGISTRY_FILE", tmp_path / "projects.json")
    deps_mod._registry = None
    ap_mod._manager = None
    yield tmp_path
    deps_mod._registry = None
    ap_mod._manager = None


@pytest.fixture
def client(tmp_data):
    from novelforge.app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def project(client):
    resp = client.post("/v1/projects", json={"name": "F6连写", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


def _open_conn(project_id):
    from novelforge.app.deps import get_registry
    return get_registry().open_conn(project_id)


def _patch_api(monkeypatch, provider):
    import novelforge.app.api.orchestrator as api_orch

    def build(cfg):
        cfg.recall.enable_summaries = False
        reg = SkillRegistry()
        register_default_skills(reg)
        gw = LLMGateway(provider, BudgetLedger(max_tokens=10_000_000, max_usd=100.0,
                                               max_revise_rounds=100))
        return Orchestrator(gw, reg, cfg)

    monkeypatch.setattr(api_orch, "_build_orch", build)


class TestBatchApi:
    def test_sync_batch_and_batch_listing(self, client, project, monkeypatch):
        script = Script()
        _patch_api(monkeypatch, LongFake(factory=script))
        r = client.post(f"/v1/{project}/pipeline/batch", json={"chapters": 2})
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["mode"], body["status"], body["error"]) == ("batch", "completed", None)
        assert [c["chapter"] for c in body["chapters"]] == [1, 2]
        assert all(c["ok"] and c["run_id"] and c["batch_id"] == body["batch_id"]
                   for c in body["chapters"])
        assert body["next_chapter"] == 3 and body["batch_tokens"] > 0
        assert script.batch_calls == [[1, 2]]
        assert client.get(f"/v1/{project}/pipeline/next").json()["next_chapter"] == 3
        # 运行历史/详情可按批次归组
        runs = client.get(f"/v1/{project}/pipeline/runs").json()
        assert {r["chapter"]: r["batch_id"] for r in runs} == {
            1: body["batch_id"], 2: body["batch_id"]}
        run1 = client.get(
            f"/v1/{project}/pipeline/runs/{body['chapters'][0]['run_id']}").json()
        assert run1["batch_id"] == body["batch_id"]

        listing = client.get(f"/v1/{project}/pipeline/batches").json()
        assert len(listing) == 1 and listing[0]["batch_id"] == body["batch_id"]
        assert listing[0]["counts"] == {"accepted": 2} and listing[0]["status"] == "completed"
        detail = client.get(f"/v1/{project}/pipeline/batches/{body['batch_id']}").json()
        assert [(c["chapter"], c["status"]) for c in detail["chapters"]] == [
            (1, "accepted"), (2, "accepted")]
        assert all(c["draft_text"] is None and c["length"] > 0 for c in detail["chapters"])
        with_text = client.get(
            f"/v1/{project}/pipeline/batches/{body['batch_id']}?include_text=true").json()
        assert with_text["chapters"][0]["draft_text"].startswith("第1章正文")
        assert client.get(f"/v1/{project}/pipeline/batches/nope").status_code == 404

    def test_request_validation_and_unknown_project(self, client, project):
        assert client.post(f"/v1/{project}/pipeline/batch",
                           json={"chapters": 31}).status_code == 422
        assert client.post("/v1/nope/pipeline/batch", json={}).status_code == 404

    def test_sync_batch_propagates_http_errors(self, client, project, monkeypatch):
        """run_batch 内部抛 HTTPException → 原样返回 HTTP 状态，而不是 200 + failed。"""
        import novelforge.control_plane.batch as batch_mod
        from fastapi import HTTPException

        def boom(*_a, **_k):
            raise HTTPException(404, "项目不存在: gone")

        monkeypatch.setattr(batch_mod, "run_batch", boom)
        r = client.post(f"/v1/{project}/pipeline/batch", json={"chapters": 2})
        assert r.status_code == 404, r.text

        def crash(*_a, **_k):
            raise RuntimeError("pipeline exploded")

        monkeypatch.setattr(batch_mod, "run_batch", crash)
        r = client.post(f"/v1/{project}/pipeline/batch", json={"chapters": 2})
        assert r.status_code == 200
        assert r.json()["status"] == "failed" and "pipeline exploded" in r.json()["error"]

    def test_stream_event_order(self, client, project, monkeypatch):
        _patch_api(monkeypatch, LongFake(factory=Script()))
        with client.stream("POST", f"/v1/{project}/pipeline/batch/stream",
                           json={"start_chapter": 1, "chapters": 2,
                                 "chapter_goals": {"2": "夜探藏经阁"}}) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            events = [json.loads(line[6:]) for line in resp.iter_lines()
                      if line.startswith("data: ")]
        kinds = [e["event"] for e in events]
        assert kinds[0] == "batch_start" and kinds[1] == "batch_draft"
        assert kinds[-1] == "batch_done" and "done" not in kinds and "error" not in kinds
        assert [e["chapter"] for e in events if e["event"] == "chapter_done"] == [1, 2]
        stage_chapters = [e["chapter"] for e in events if e["event"] == "stage"]
        assert stage_chapters == sorted(stage_chapters) and set(stage_chapters) == {1, 2}
        # 第 1 章所有 stage 都在其 chapter_done 之前
        done1 = next(i for i, e in enumerate(events)
                     if e["event"] == "chapter_done" and e["chapter"] == 1)
        assert all(e["chapter"] == 2 for e in events[done1:] if e["event"] == "stage")
        assert events[-1]["status"] == "completed" and events[-1]["mode"] == "batch"

    def test_stream_disconnect_stops_after_current_chapter(self, client, project, monkeypatch):
        """SSE 客户端断开 → 当前章完成后停止，剩余章作废（TestClient 不传播断开，直接驱动生成器）。"""
        import asyncio

        import novelforge.app.api.orchestrator as api_orch
        from novelforge.app.deps import get_registry
        from novelforge.app.models import BatchRunRequest

        _patch_api(monkeypatch, LongFake(factory=Script()))

        async def scenario() -> str:
            resp = await api_orch.pipeline_batch_stream(
                project, BatchRunRequest(chapters=3), get_registry())
            stream = resp.body_iterator
            async for chunk in stream:
                if '"event": "chapter_done"' in chunk:
                    break
            await stream.aclose()                       # 模拟客户端断开
            status = "running"
            for _ in range(400):
                conn = _open_conn(project)
                status = conn.execute("SELECT status FROM draft_batches").fetchone()[0]
                conn.close()
                if status != "running":
                    break
                await asyncio.sleep(0.05)
            return status

        assert asyncio.run(scenario()) == "canceled"
        conn = _open_conn(project)
        try:
            rows = conn.execute("SELECT chapter, status, reason FROM draft_batch_chapters"
                                " ORDER BY chapter").fetchall()
        finally:
            conn.close()
        assert rows[0]["status"] == "accepted"
        assert (rows[-1]["status"], rows[-1]["reason"]) == ("discarded", "canceled")

    def test_stream_sequential_mode_without_long_output(self, client, project, monkeypatch):
        _patch_api(monkeypatch, FakeProvider(factory=Script()))
        with client.stream("POST", f"/v1/{project}/pipeline/batch/stream",
                           json={"chapters": 2}) as resp:
            events = [json.loads(line[6:]) for line in resp.iter_lines()
                      if line.startswith("data: ")]
        assert events[0]["event"] == "batch_start" and events[0]["mode"] == "sequential"
        assert events[-1]["event"] == "batch_done" and events[-1]["status"] == "completed"
        assert client.get(f"/v1/{project}/pipeline/batches").json() == []

    def test_registry_first_access_sweeps_stale_batches(self, client, project):
        import novelforge.app.deps as deps_mod

        conn = _open_conn(project)
        _insert_stale_batch(conn)
        conn.close()
        deps_mod._registry = None      # 模拟进程重启：新注册表首次打开项目
        conn = _open_conn(project)
        try:
            _assert_swept(conn)
        finally:
            conn.close()


def _wait_finish(client, project, sid, timeout=30.0):
    deadline = time.time() + timeout
    s = None
    while time.time() < deadline:
        s = client.get(f"/v1/{project}/autopilot/{sid}").json()
        if s["status"] not in ("running", "degraded"):
            return s
        time.sleep(0.05)
    return s


def _patch_autopilot_gateway(monkeypatch, provider):
    import novelforge.control_plane.llm.factory as factory_mod
    monkeypatch.setattr(factory_mod, "build_gateway", lambda cfg, ledger=None: LLMGateway(
        provider, BudgetLedger(max_tokens=10**8, max_usd=1e4, max_revise_rounds=1000)))


class TestAutopilotBatch:
    def test_autopilot_batch_path(self, client, project, monkeypatch):
        script = Script()
        _patch_autopilot_gateway(monkeypatch, LongFake(factory=script))
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 3, "mode": "auto_promote"})
        assert r.status_code == 202
        sid = r.json()["session_id"]
        s = _wait_finish(client, project, sid)
        assert s["status"] == "completed", s
        assert (s["chapters_done"], s["current_chapter"]) == (3, 4)
        assert script.batch_calls == [[1, 2, 3]]

        conn = _open_conn(project)
        try:
            runs = conn.execute(
                "SELECT chapter, batch_id, tokens_spent, usd_spent FROM pipeline_run"
                " WHERE status='completed' ORDER BY chapter").fetchall()
            batch = conn.execute("SELECT id, origin, status FROM draft_batches").fetchone()
            row = conn.execute("SELECT budget_tokens_total, budget_usd_total, req_json"
                               " FROM autopilot_sessions WHERE session_id=?", (sid,)).fetchone()
        finally:
            conn.close()
        assert [r["chapter"] for r in runs] == [1, 2, 3]
        assert {r["batch_id"] for r in runs} == {batch["id"]}
        assert (batch["origin"], batch["status"]) == ("autopilot", "completed")
        # 会话预算 = 连写调用 + 逐章流水线 = 各章 pipeline_run（已摊入连写成本）之和
        assert s["budget_tokens_total"] == sum(r["tokens_spent"] for r in runs) > 0
        assert row["budget_tokens_total"] == s["budget_tokens_total"]
        assert row["budget_usd_total"] == pytest.approx(sum(r["usd_spent"] for r in runs), abs=1e-4)
        assert json.loads(row["req_json"])["batch_size"] is None

    def test_autopilot_batch_stops_with_error_after_max_regenerations(
            self, client, project, monkeypatch):
        script = Script(batch=lambda n, chs: batch_output(chs, short={2}))
        _patch_autopilot_gateway(monkeypatch, LongFake(factory=script))
        sid = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 3, "mode": "auto_promote"}).json()["session_id"]
        s = _wait_finish(client, project, sid)
        assert s["status"] == "error" and "max regenerations" in s["last_error"]
        assert (s["chapters_done"], s["current_chapter"]) == (1, 2)   # 断点 = 首个未接受章
        assert script.batch_calls == [[1, 2, 3], [2, 3], [2, 3]]       # 默认最多重写 2 次
        conn = _open_conn(project)
        try:
            windows = [r[0] for r in conn.execute(
                "SELECT tokens_spent FROM draft_batches ORDER BY created_at, rowid")]
            runs = sum(r[0] for r in conn.execute(
                "SELECT tokens_spent FROM pipeline_run WHERE status='completed'"))
        finally:
            conn.close()
        # 会话预算含全部连写调用：首窗成本已摊入第 1 章的 run，后两窗无接受章、单独计入
        assert s["budget_tokens_total"] == runs + sum(windows[1:])

    def test_autopilot_batch_pauses_on_consecutive_blocks(self, client, project, monkeypatch):
        script = Script(revise=lambda n: chapter_body(9))
        script.planner = lambda ch, n: "[]"            # 无节拍 → 每章残留 beat_contract block
        _patch_autopilot_gateway(monkeypatch, LongFake(factory=script))
        sid = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 4, "mode": "auto_promote"}).json()["session_id"]
        s = _wait_finish(client, project, sid)
        assert s["status"] == "paused" and s["policy_mode"] == "human_gate"
        assert (s["chapters_done"], s["current_chapter"]) == (2, 3)
        assert script.batch_calls == [[1, 2, 3, 4], [2, 3, 4]]
        # 暂停在逐章回调里广播；连写线程随后（无 LLM 调用）收尾批次行
        deadline = time.time() + 10
        while True:
            conn = _open_conn(project)
            try:
                statuses = [r[0] for r in conn.execute(
                    "SELECT status FROM draft_batches ORDER BY created_at, rowid")]
                chapters = [r[0] for r in conn.execute(
                    "SELECT chapter FROM draft_index ORDER BY rowid")]
            finally:
                conn.close()
            if "running" not in statuses or time.time() > deadline:
                break
            time.sleep(0.05)
        assert statuses == ["partial", "partial"]     # 第 2 窗：第 2 章接受，3-4 章作废后停
        assert chapters == [1, 2]

    def test_autopilot_batch_size_one_keeps_legacy_loop(self, client, project, monkeypatch):
        script = Script()
        fake = LongFake(factory=script)
        _patch_autopilot_gateway(monkeypatch, fake)
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 2, "mode": "auto_promote", "batch_size": 1})
        sid = r.json()["session_id"]
        s = _wait_finish(client, project, sid)
        assert s["status"] == "completed" and s["chapters_done"] == 2
        assert script.batch_calls == []
        assert sum("本章任务" in u for u in users(fake)) == 2
        conn = _open_conn(project)
        try:
            assert [r[0] for r in conn.execute("SELECT batch_id FROM pipeline_run")] == [None, None]
            req = json.loads(conn.execute("SELECT req_json FROM autopilot_sessions"
                                          " WHERE session_id=?", (sid,)).fetchone()[0])
        finally:
            conn.close()
        assert req["batch_size"] == 1

    def test_batch_size_persisted_and_resumed(self, client, project, monkeypatch):
        import novelforge.control_plane.llm.factory as factory_mod
        from novelforge.control_plane.orchestrator import ChapterOutcome

        def fake_generate(self, chapter, conn, *, chapter_goal="", **kw):
            return ChapterOutcome(chapter=chapter, ok=True, usage_tokens=10, usage_usd=0.001)

        monkeypatch.setattr(Orchestrator, "generate_chapter", fake_generate)
        monkeypatch.setattr(factory_mod, "build_gateway", lambda cfg, ledger=None: None)
        conn = _open_conn(project)
        conn.execute(
            "INSERT INTO autopilot_sessions(session_id, project_id, from_chapter, to_chapter,"
            " current_chapter, status, policy_mode, chapters_done, req_json, started_at)"
            " VALUES('aps_b', ?, 1, 4, 3, 'interrupted', 'auto_promote', 2,"
            " '{\"mode\":\"auto_promote\",\"batch_size\":4}', '2026-01-01T00:00:00')",
            (project,),
        )
        conn.commit()
        conn.close()
        r = client.post(f"/v1/{project}/autopilot/aps_b/resume")
        assert r.status_code == 202
        new_sid = r.json()["session_id"]
        assert _wait_finish(client, project, new_sid)["status"] == "completed"
        conn = _open_conn(project)
        try:
            req = json.loads(conn.execute("SELECT req_json FROM autopilot_sessions"
                                          " WHERE session_id=?", (new_sid,)).fetchone()[0])
        finally:
            conn.close()
        assert req["batch_size"] == 4
