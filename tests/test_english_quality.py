"""Deterministic English metrics, craft checks and candidate selection."""
from __future__ import annotations

import sqlite3

import pytest

from novelforge.craft.candidate_judge import score_chapter_detailed, select_best
from novelforge.craft.language_quality import check_english_quality
from novelforge.craft.voice import check_voice_distinctness
from novelforge.text_metrics import (
    count_characters,
    count_content_units,
    count_words,
    language_matches,
)


def _profile(**quality_overrides):
    quality = {
        "chapter_word_range": [20, 40],
        "hard_min_words": 10,
        "max_pov_shifts": 0,
        "tense_consistency": True,
        "requires_progress_event": True,
        "content_warnings": [],
        "readability_min_words": 20,
    }
    quality.update(quality_overrides)
    return {
        "content_language": "en-US",
        "writing_profile_id": "en_progression_serial_v1",
        "writing_profile_version": 1,
        "profile_id": "en_progression_serial_v1",
        "profile_version": "1.0.0",
        "prompt_version": "1.0.0",
        "prompt_rules": {
            "pov": "third_person_limited",
            "tense": "past",
            "pace": "fast",
            "prose_density": "accessible",
            "progress_feedback": "frequent",
            "chapter_end": "forward-driving hook",
        },
        "quality_rules": quality,
    }


class TestTextMetrics:
    def test_english_count_keeps_contractions_and_compounds_together(self):
        text = "Don't stop the dragon-rider, O’Malley. Version 1,000.5 works."
        assert count_words(text) == 8
        assert count_content_units(text, "en-US") == 8

    def test_chinese_content_units_preserve_legacy_len_semantics(self):
        text = "长 剑"
        assert count_characters(text) == len(text) == 3
        assert count_content_units(text, "zh-CN") == len(text)

    def test_language_match_allows_small_cross_script_terms(self):
        assert language_matches("The artifact is called 天门, and it opens at dawn.", "en-US")
        assert not language_matches("这是一个完全中文的章节。", "en-US")
        assert not language_matches(
            "El héroe caminó hacia la puerta, pero la noche ocultaba una amenaza.",
            "en-US",
        )
        assert language_matches("这是 API 测试。", "zh-CN")
        assert not language_matches("", "en-US")


class TestEnglishQuality:
    def test_mechanical_profile_findings_are_source_anchored(self):
        text = (
            "I walk to the colour gate. I am afraid. "
            "For a moment the silent guard watches me. "
            "The silent guard says nothing. The silent guard turns away. "
            "Blood sprayed across the wall. The room remains still."
        )
        findings = check_english_quality(text, _profile())
        categories = {finding["category"] for finding in findings}
        assert {
            "pov_consistency", "tense_consistency", "spelling_variant",
            "style_adherence", "content_warning_match",
        } <= categories
        for finding in findings:
            assert finding["evidence"]
            assert finding["evidence"] in text

    def test_language_length_dialogue_and_repetition_findings(self):
        profile = _profile(chapter_word_range=[50, 60], hard_min_words=40)
        chinese = "这是中文正文，不应被误认为英文。"
        chinese_findings = check_english_quality(chinese, profile)
        assert {f["category"] for f in chinese_findings} >= {
            "language_match", "word_count_range",
        }
        assert next(f for f in chinese_findings if f["category"] == "language_match")["severity"] == "block"

        text = (
            "The old sentinel waited. The old sentinel listened. "
            "The old sentinel trembled. 「Who is there?」"
        )
        findings = check_english_quality(text, profile)
        categories = {finding["category"] for finding in findings}
        assert {"dialogue_format", "repetition", "word_count_range"} <= categories
        assert all(finding["evidence"] in text for finding in findings if finding["evidence"])

    def test_readability_is_advisory_only(self):
        dense_sentence = (
            "Notwithstanding the incomprehensibility of institutionalized characterization, "
            "interdisciplinary representatives systematically reconceptualized disproportionate "
            "methodologies while simultaneously internationalizing counterrevolutionary "
            "administrative responsibilities and indistinguishable philosophical abstractions"
        )
        text = dense_sentence + ". " + dense_sentence + "."
        findings = check_english_quality(
            text,
            _profile(
                chapter_word_range=[20, 100],
                hard_min_words=10,
                requires_progress_event=False,
                readability_min_words=20,
            ),
        )
        readability = [f for f in findings if f["category"] == "readability"]
        assert readability and readability[0]["severity"] == "warn"
        assert readability[0]["evidence"] in text

    def test_explicit_speaker_switch_requires_new_paragraph(self):
        text = '"Stay here," Alice said. "I refuse," Bob replied.'
        findings = check_english_quality(
            text,
            _profile(
                chapter_word_range=[5, 30], hard_min_words=3,
                requires_progress_event=False, readability_min_words=100,
            ),
        )
        dialogue = [f for f in findings if f["category"] == "dialogue_format"]
        assert dialogue
        assert dialogue[0]["evidence"] in text

    def test_declared_content_warning_suppresses_warning(self):
        text = "The blade turned, and blood sprayed across the stones before he advanced. Why now?"
        findings = check_english_quality(
            text,
            _profile(
                chapter_word_range=[10, 30],
                hard_min_words=5,
                content_warnings=["graphic_violence"],
            ),
        )
        assert "content_warning_match" not in {f["category"] for f in findings}


class TestEnglishVoice:
    def test_english_overlap_uses_words_not_shared_letters(self):
        text = (
            '"Character chapter alpha."\n'
            '"Charter chatter beta."\n'
            '"Different wording gamma."\n'
            '"Entirely separate delta."'
        )
        assert check_voice_distinctness(text, [], language="en-US") == []

    def test_repeated_english_dialogue_has_evidence(self):
        line = "We leave before the red moon rises"
        text = "\n".join(f'"{line}."' for _ in range(4))
        issues = check_voice_distinctness(text, [], language="en-US")
        assert issues and issues[0].span in text


class TestEnglishCandidateSelection:
    def test_target_range_beats_longer_out_of_range_candidate(self):
        profile = _profile(
            chapter_word_range=[20, 30], hard_min_words=10,
            requires_progress_event=False, readability_min_words=100,
        )
        in_range = " ".join(["river"] * 25)
        overlong = " ".join(["mountain"] * 60)
        report = select_best(
            [{"draft_text": in_range, "proposals": []},
             {"draft_text": overlong, "proposals": []}],
            writing_profile=profile,
        )
        assert report["winner"] == 0
        assert report["eligible"] == [0, 1]
        assert report["word_counts"] == [25, 60]

    def test_continuity_veto_precedes_target_range_fit(self, monkeypatch):
        import novelforge.craft.candidate_judge as judge

        profile = _profile(
            chapter_word_range=[20, 30], hard_min_words=10,
            requires_progress_event=False, readability_min_words=100,
        )
        in_range_with_conflict = "dirty " + " ".join(["river"] * 24)
        just_outside_clean = "clean " + " ".join(["road"] * 18)
        monkeypatch.setattr(
            judge, "_count_hard_blocks",
            lambda text, world: 1 if "dirty" in text else 0,
        )

        report = select_best(
            [
                {"draft_text": in_range_with_conflict, "proposals": []},
                {"draft_text": just_outside_clean, "proposals": []},
            ],
            world=object(), writing_profile=profile,
        )

        assert report["eligible"] == [0, 1]
        assert report["winner"] == 1
        assert report["reason"] == "hard_block_veto"

    def test_judge_absence_does_not_pick_longest_english_draft(self):
        profile = _profile(
            chapter_word_range=[20, 30], hard_min_words=10,
            requires_progress_event=False, readability_min_words=100,
        )
        shorter = " ".join(["river"] * 24)
        longer = " ".join(["mountain"] * 29)
        report = select_best(
            [{"draft_text": shorter, "proposals": []},
             {"draft_text": longer, "proposals": []}],
            writing_profile=profile,
        )
        assert report["winner"] == 0
        assert report["reason"] == "judge_unavailable_profile_fit"

    def test_profile_adherence_breaks_tie(self):
        profile = _profile(
            chapter_word_range=[10, 30], hard_min_words=5,
            requires_progress_event=False, readability_min_words=100,
        )
        clean = "He advanced past the color gate, and the watchman stepped aside. What waited beyond?"
        mixed = "He advanced past the colour gate, and the watchman stepped aside. What waited beyond?"
        report = select_best(
            [{"draft_text": clean, "proposals": []},
             {"draft_text": mixed, "proposals": []}],
            writing_profile=profile,
        )
        assert report["winner"] == 0
        assert report["reason"] == "profile_adherence_veto"
        assert report["profile_adherence_scores"][0] > report["profile_adherence_scores"][1]

    def test_all_failed_english_prefers_language_fit_not_longest(self):
        profile = _profile(
            chapter_word_range=[20, 30], hard_min_words=10,
            requires_progress_event=False, readability_min_words=100,
        )
        english_short = "Only eight English words remain before the final gate."
        chinese_long = "这是更长的中文候选稿。" * 50
        report = select_best(
            [{"draft_text": english_short, "proposals": []},
             {"draft_text": chinese_long, "proposals": []}],
            writing_profile=profile,
        )
        assert report["winner"] == 0
        assert report["reason"] == "english_prescreen_all_failed_best_fit"

    def test_point_score_uses_english_profile_prompt(self):
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.llm.fake_provider import FakeProvider
        from novelforge.control_plane.llm.gateway import LLMGateway

        provider = FakeProvider(responses=[
            '{"score":8.2,"dimensions":{"hook":8,"pacing":8,'
            '"character":8,"prose":8.8},"reason":"Strong fit"}',
        ])
        gateway = LLMGateway(provider, BudgetLedger(max_tokens=10_000, max_usd=1))

        detail = score_chapter_detailed(
            gateway,
            "mid",
            "Mara must choose whether to enter.",
            "Mara reached the gate and made her choice. What waited beyond?",
            _profile(),
        )

        assert detail and detail["score"] == 8.2
        assert provider.calls[0]["messages"][0].content.startswith("Chapter goal: ")
        assert "locked writing profile" in provider.calls[0]["system"].lower()


@pytest.fixture
def conn():
    from novelforge.db.connection import init_db_from_conn

    value = sqlite3.connect(":memory:")
    value.row_factory = sqlite3.Row
    init_db_from_conn(value)
    yield value
    value.close()


def _fake_gateway():
    from novelforge.control_plane.llm.provider import CapabilitySet, Response, Usage

    class Gateway:
        def generate(self, *args, **kwargs):
            return Response(text="[]", tool_calls=[], usage=Usage(0, 0, 0, 0, "fake", "fake"))

        def model_for(self, tier):
            return "fake"

        class _provider:
            @staticmethod
            def capabilities(model):
                return CapabilitySet(
                    supports_tools=False, supports_cache=False,
                    max_tokens_out=4096, context_window=8192,
                )

    return Gateway()


def test_craft_skill_emits_english_findings_and_skips_legacy_payoff(conn):
    from novelforge.control_plane.skill_base import SkillContext
    from novelforge.skills.craft_check_skill import CraftCheckSkill

    profile = _profile(chapter_word_range=[20, 30], hard_min_words=10)
    ctx = SkillContext(
        project_id="english", target_chapter=1, mode="human_gate", as_of_chapter=0,
        budget=None, llm=_fake_gateway(), conn=conn,
        workspace={
            "draft_text": "He stopped at the gate.",
            "beats": [
                {"beat_type": "payoff_beat", "value_axis": "progress", "summary": "A reward"},
                {"beat_type": "hook", "value_axis": "risk", "summary": "A new threat"},
            ],
            "proposals": [],
            "writing_profile": profile,
        },
    )
    result = CraftCheckSkill().run(ctx)
    by_check = {item["check"]: item for item in ctx.workspace["craft_issues"]}
    assert "payoff" not in by_check
    assert by_check["word_count_range"]["severity"] == "block"
    assert by_check["word_count_range"]["evidence"] in ctx.workspace["draft_text"]
    assert not result.ok


def test_craft_skill_resolves_effective_profile_from_project_db(conn):
    from novelforge.control_plane.skill_base import SkillContext
    from novelforge.skills.craft_check_skill import CraftCheckSkill
    from novelforge.writing_profiles import initialize_project_writing_profile

    initialize_project_writing_profile(conn, {
        "content_language": "en-US",
        "target_market": "global_english",
        "publication_format": "web_serial",
        "primary_genre": "fantasy",
        "writing_profile_id": "en_progression_serial_v1",
    })
    ctx = SkillContext(
        project_id="english", target_chapter=1, mode="human_gate", as_of_chapter=0,
        budget=None, llm=_fake_gateway(), conn=conn,
        workspace={
            "draft_text": "这是错误语言的正文。",
            "beats": [
                {"beat_type": "setup", "value_axis": "risk", "summary": "A risk"},
                {"beat_type": "hook", "value_axis": "choice", "summary": "A choice"},
            ],
            "proposals": [],
        },
    )

    result = CraftCheckSkill().run(ctx)

    by_check = {item["check"]: item for item in ctx.workspace["craft_issues"]}
    assert by_check["language_match"]["severity"] == "block"
    assert not result.ok


def test_chapter_draft_retries_wrong_language_before_accepting(conn):
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway
    from novelforge.control_plane.skill_base import SkillContext
    from novelforge.skills.chapter_draft_skill import ChapterDraftSkill

    valid = " ".join(
        "Mara advanced through the gate and chose the dangerous road before dawn".split()
        + ["forward"] * 10
    )
    provider = FakeProvider(responses=[
        "```draft\n这是长度足够但语言错误的中文草稿。\n```",
        f"```draft\n{valid}\n```",
    ])
    ledger = BudgetLedger(max_tokens=100_000, max_usd=10)
    profile = _profile(
        chapter_word_range=[20, 40], hard_min_words=10,
        requires_progress_event=False, readability_min_words=100,
    )
    ctx = SkillContext(
        project_id="english", target_chapter=1, mode="human_gate", as_of_chapter=0,
        budget=ledger, llm=LLMGateway(provider, ledger), conn=conn,
        workspace={
            "writing_profile": profile,
            "beats": [
                {"beat_type": "setup", "summary": "Mara reaches the gate"},
                {"beat_type": "hook", "summary": "She chooses a road"},
            ],
        },
    )

    result = ChapterDraftSkill().run(ctx)

    assert result.ok
    assert ctx.workspace["draft_text"] == valid
    assert len(provider.calls) == 2


def test_orchestrator_never_persists_last_wrong_language_response(conn):
    from novelforge.config import NovelForgeConfig
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway
    from novelforge.control_plane.orchestrator import Orchestrator
    from novelforge.control_plane.skill_registry import SkillRegistry
    from novelforge.skills import register_default_skills
    from novelforge.writing_profiles import initialize_project_writing_profile

    initialize_project_writing_profile(conn, {
        "content_language": "en-US",
        "target_market": "global_english",
        "publication_format": "web_serial",
        "primary_genre": "fantasy",
        "writing_profile_id": "en_progression_serial_v1",
    })

    def response_for(messages, **_kwargs):
        user = str(messages[-1].content)
        if "## Planning Task" in user:
            return (
                '[{"beat_type":"setup","summary":"Mara reaches the gate",'
                '"value_axis":"safety to danger"},'
                '{"beat_type":"turn","summary":"The keeper names a price",'
                '"value_axis":"hope to doubt"},'
                '{"beat_type":"tension_point","summary":"Mara accepts the bargain",'
                '"value_axis":"freedom to obligation"},'
                '{"beat_type":"hook","summary":"The sealed road opens",'
                '"value_axis":"certainty to mystery"}]'
            )
        if "## Chapter Task" in user:
            return "```draft\n" + ("这是错误语言的正文。" * 700) + "\n```"
        return "[]"

    provider = FakeProvider(factory=response_for)
    ledger = BudgetLedger(max_tokens=1_000_000, max_usd=20)
    registry = SkillRegistry()
    register_default_skills(registry)
    cfg = NovelForgeConfig(project_id="english")
    cfg.candidates.n_candidates = 1
    cfg.quality.enabled = False
    cfg.settle.enabled = False
    outcome = Orchestrator(
        LLMGateway(provider, ledger), registry, cfg,
    ).generate_chapter(1, conn)

    assert not outcome.ok
    assert "writing-profile constraints" in (outcome.error or "")
    assert conn.execute("SELECT COUNT(*) FROM draft_index").fetchone()[0] == 0
    run = conn.execute(
        "SELECT status, detail_json FROM pipeline_run WHERE run_id=?",
        (outcome.run_id,),
    ).fetchone()
    assert run["status"] == "crashed"
    assert "writing_profile_constraints" in run["detail_json"]
