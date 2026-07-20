"""English writing-profile propagation through L2, L3 and continuity review."""
from __future__ import annotations

import json

import pytest

from novelforge.control_plane.budget import BudgetLedger
from novelforge.control_plane.llm.fake_provider import FakeProvider
from novelforge.control_plane.llm.gateway import LLMGateway
from novelforge.db.connection import connect, init_db_from_conn, set_meta
from novelforge.memory.l2_aggregate import aggregate_chapter_scenes, run_l2_window
from novelforge.memory.l3_events import extract_canon_events, scan_canon_signals
from novelforge.text_metrics import count_words


@pytest.fixture
def conn():
    value = connect(":memory:")
    init_db_from_conn(value)
    yield value
    value.close()


def _profile() -> dict:
    return {
        "content_language": "en-US",
        "target_market": "global_english",
        "publication_format": "web_serial",
        "profile_id": "en_progression_serial_v1",
        "profile_version": 1,
        "prompt_version": "1.0.0",
        "prompt_rules": {
            "pov": "third_person_limited",
            "tense": "past",
            "pace": "fast",
            "prose_density": "accessible",
            "dialogue_density": "medium",
            "chapter_end": "forward-driving hook",
        },
        "quality_rules": {
            "chapter_word_range": [1500, 2500],
            "hard_min_words": 1200,
            "tense_consistency": True,
            "requires_progress_event": True,
        },
    }


def _persist_english_profile(conn) -> None:
    values = {
        "content_language": "en-US",
        "target_market": "global_english",
        "publication_format": "web_serial",
        "writing_profile_id": "en_progression_serial_v1",
        "writing_profile_version": "1",
        "writing_profile_effective_from_chapter": "1",
    }
    for key, value in values.items():
        set_meta(conn, key, value)
    conn.commit()


def _gateway(response: str):
    provider = FakeProvider(responses=[response])
    gateway = LLMGateway(provider, BudgetLedger(max_tokens=200_000, max_usd=5.0))
    return gateway, provider


class TestEnglishL2:
    def test_auto_resolves_project_profile_and_uses_english_prompt(self, conn):
        _persist_english_profile(conn)
        gateway, provider = _gateway(
            "Mara reaches the Aether Gate and trades her last map for entry."
        )
        source = (
            "Mara reached the Aether Gate at dusk. The guard refused her coin, "
            "so she surrendered her last map and stepped inside."
        )
        assert aggregate_chapter_scenes(conn, gateway, 1, source) == 1
        call = provider.calls[0]
        assert "scene continuity summarizer" in call["system"]
        assert "Locked Writing Profile" in call["system"]
        assert "en_progression_serial_v1" in call["system"]
        assert "Scene source" in str(call["messages"][-1].content)
        summary = conn.execute(
            "SELECT summary FROM l2_scenes WHERE chapter=1"
        ).fetchone()["summary"]
        assert summary.startswith("Mara reaches")

    def test_wrong_language_model_summary_falls_back_to_english_source(self, conn):
        gateway, _ = _gateway("这是错误语言的摘要。")
        source = "Mara opens the Aether Gate and discovers the city beyond it."
        aggregate_chapter_scenes(
            conn, gateway, 2, source, writing_profile=_profile(),
        )
        summary = conn.execute(
            "SELECT summary FROM l2_scenes WHERE chapter=2"
        ).fetchone()["summary"]
        assert summary == source
        assert "错误语言" not in summary

    def test_english_offline_fallback_uses_words_not_characters(self, conn):
        source = " ".join(f"word{i}" for i in range(100))
        aggregate_chapter_scenes(
            conn, None, 3, source, writing_profile=_profile(),
        )
        summary = conn.execute(
            "SELECT summary FROM l2_scenes WHERE chapter=3"
        ).fetchone()["summary"]
        assert count_words(summary) == 80
        assert len(summary) > 80

    def test_maintenance_rebuild_uses_profile_effective_for_each_chapter(self, conn):
        from novelforge.writing_profiles import (
            initialize_project_writing_profile,
            update_project_writing_profile,
        )

        initialize_project_writing_profile(conn, {
            "content_language": "en-US",
            "target_market": "global_english",
            "publication_format": "web_serial",
            "primary_genre": "fantasy",
            "writing_profile_id": "en_progression_serial_v1",
        })
        update_project_writing_profile(
            conn,
            {
                "writing_profile_id": "en_cozy_fantasy_v1",
                "story_tags": ["cozy"],
            },
            effective_from_chapter=2,
        )
        for chapter in (1, 2):
            conn.execute(
                "INSERT INTO draft_index("
                "id, chapter, revision_round, file_path, sha256, status)"
                " VALUES(?,?,?,?,?,'committed')",
                (f"draft_{chapter}", chapter, 0, f"/tmp/{chapter}.md", "x"),
            )
        conn.commit()
        provider = FakeProvider(responses=[
            "Mara crosses the gate and gains a dangerous new rank.",
            "Mara shares tea with the village baker and finds a home.",
        ])
        gateway = LLMGateway(
            provider, BudgetLedger(max_tokens=200_000, max_usd=5.0),
        )
        texts = {
            1: "Mara crossed the gate and gained a dangerous new rank.",
            2: "Mara shared tea with the village baker and found a home.",
        }

        result = run_l2_window(conn, gateway, 2, texts.get)

        assert result["processed"] == 2
        systems = [call["system"] for call in provider.calls]
        assert "en_progression_serial_v1" in systems[0]
        assert "en_cozy_fantasy_v1" in systems[1]


class TestEnglishL3:
    def test_scans_english_rule_prophecy_constraint_and_named_entity(self):
        text = (
            "The law of magic demands a memory for every spell. "
            "A prophecy warned that one day the western sun would fail. "
            "Speaking the king's true name is forbidden. "
            "They called Aether Gate the last safe passage."
        )
        signals = scan_canon_signals(
            text, set(), writing_profile=_profile(),
        )
        assert {signal["type"] for signal in signals} == {
            "world_rule", "foreshadowing", "constraint", "new_entity",
        }
        assert any(signal["anchor"] == "Aether Gate" for signal in signals)

    def test_known_english_entity_is_case_insensitive(self):
        text = "They called Aether Gate the last safe passage."
        signals = scan_canon_signals(
            text, {"aether gate"}, writing_profile=_profile(),
        )
        assert not any(signal["type"] == "new_entity" for signal in signals)

    def test_auto_profile_propagates_to_prompt_and_preserves_evidence(self, conn):
        _persist_english_profile(conn)
        text = "The law of magic demanded one memory for every spell."
        atoms = [{
            "type": "world_rule",
            "name": None,
            "statement": "Every spell costs one memory.",
            "evidence": "one memory for every spell",
        }]
        gateway, provider = _gateway(json.dumps(atoms))
        proposals = extract_canon_events(conn, gateway, 4, text)
        assert len(proposals) == 1
        assert proposals[0]["new"]["statement"] == "Every spell costs one memory."
        assert proposals[0]["evidence_refs"][0]["quote"] in text
        assert proposals[0]["reason"].startswith("L3 event-driven")
        call = provider.calls[0]
        assert "canonical-event extractor" in call["system"]
        assert "Locked Writing Profile" in call["system"]
        assert "Chapter source" in str(call["messages"][-1].content)

    def test_wrong_language_normalized_statement_is_rejected(self, conn):
        text = "The law of magic demanded one memory for every spell."
        atoms = [{
            "type": "world_rule",
            "name": None,
            "statement": "每个法术消耗一段记忆。",
            "evidence": "one memory for every spell",
        }]
        gateway, _ = _gateway(json.dumps(atoms, ensure_ascii=False))
        assert extract_canon_events(
            conn, gateway, 5, text, writing_profile=_profile(),
        ) == []


class TestEnglishContinuity:
    def _context(self, conn, response: str):
        from novelforge.control_plane.skill_base import SkillContext

        gateway, provider = _gateway(response)
        draft = "Mara knew the sealed name, although nobody had told her."
        ctx = SkillContext(
            project_id="english", target_chapter=2, mode="human_gate",
            as_of_chapter=1, budget=gateway.ledger, llm=gateway, conn=conn,
            workspace={
                "draft_text": draft,
                "proposals": [],
                "writing_profile": _profile(),
                "stable_context": "## Canon\nMara has never learned the sealed name.",
            },
        )
        return ctx, provider, draft

    def test_english_soft_check_receives_profile_and_keeps_anchored_finding(self, conn):
        from novelforge.skills.continuity_check_skill import ContinuityCheckSkill

        response = json.dumps([{
            "category": "2.2-impossible_knowledge",
            "severity": "block",
            "issue": "Mara knows a secret she never learned.",
            "evidence": "Mara knew the sealed name",
            "fix": "Show how Mara learned the name before this moment.",
            "repair_scope": "local",
        }])
        ctx, provider, draft = self._context(conn, response)
        ContinuityCheckSkill().run(ctx)
        findings = [
            item for item in ctx.workspace["continuity_issues"]
            if item.get("source") == "llm_soft"
        ]
        assert len(findings) == 1
        assert findings[0]["evidence"] in draft
        call = provider.calls[0]
        assert "continuity editor" in call["system"]
        assert "POV: third_person_limited" in call["system"]
        assert "Draft chapter" in str(call["messages"][-1].content)

    def test_wrong_language_soft_finding_is_dropped(self, conn):
        from novelforge.skills.continuity_check_skill import ContinuityCheckSkill

        response = json.dumps([{
            "category": "2.2-impossible_knowledge",
            "severity": "warn",
            "issue": "角色知道了不该知道的秘密。",
            "evidence": "Mara knew the sealed name",
            "fix": "补充她获知秘密的过程。",
            "repair_scope": "local",
        }], ensure_ascii=False)
        ctx, _, _ = self._context(conn, response)
        ContinuityCheckSkill().run(ctx)
        assert not any(
            item.get("source") == "llm_soft"
            for item in ctx.workspace["continuity_issues"]
        )


def test_english_recall_and_plan_renderers_do_not_inject_chinese_labels():
    from novelforge.memory.recall import RecallPack
    from novelforge.planning.context import PlanContext

    recall = RecallPack(
        entities=[{"canonical_name": "Mara", "entity_type": "character"}],
        chapter_summaries=[{"chapter": 1, "summary": "Mara found the gate."}],
        volume_summary="Mara is searching for the lost road.",
    )
    plan = PlanContext(
        target_chapter=2,
        story_plan={
            "title": "The Lost Road",
            "premise": "Mara must reopen a sealed road.",
            "ending_direction": "The road reconnects two divided cities.",
        },
        current_contract={
            "chapter": 2,
            "title": "The Toll",
            "goal": "Mara chooses what she will sacrifice.",
            "must_advance": ["The gate bargain"],
            "must_not_reveal": ["The keeper's true name"],
            "beats": [],
        },
        lookahead=[{"chapter": 3, "goal": "Pay the cost"}],
    )

    rendered = "\n".join([
        recall.to_stable_context_str("en-US"),
        recall.to_dynamic_context_str("en-US"),
        plan.render_for_planner("en-US"),
        plan.render_contract_for_draft("en-US"),
    ])

    assert "Core entities" in rendered
    assert "Recent chapter summaries" in rendered
    assert "Locked chapter contract" in rendered
    assert "Must not reveal" in rendered
    assert not any(label in rendered for label in ("第2章", "全书方向", "本章硬契约", "禁止揭示"))
