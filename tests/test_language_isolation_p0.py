from __future__ import annotations

import json
import sqlite3


def _db() -> sqlite3.Connection:
    from novelforge.db.connection import init_db_from_conn

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return init_db_from_conn(conn)


def test_english_tokenizer_is_unicode_aware_and_never_emits_cjk():
    from novelforge.tokenizer import cut, tokenize, tokenizer_version

    source = "The state-of-the-art knight didn’t enter 青云山. Café déjà vu."

    assert cut(source, "en-US") == [
        "the", "state-of-the-art", "knight", "didn't", "enter",
        "café", "déjà", "vu",
    ]
    assert tokenize(source, "en-US") == (
        "the stateoftheart state art knight didnt didn enter "
        "café déjà vu"
    )
    assert tokenizer_version("en-US").startswith("en-US:unicode-latin-word-4-lemma")
    assert tokenizer_version("en-US") != tokenizer_version("zh-CN")


def test_english_draft_fts_uses_same_index_and_query_representation():
    from novelforge.app.api.memory import _fts_escape
    from novelforge.db.connection import index_draft_fts, set_meta

    conn = _db()
    try:
        set_meta(conn, "content_language", "en-US")
        index_draft_fts(
            conn, "draft_en", 1,
            "Mara's state-of-the-art compass pointed north.",
        )
        row = conn.execute(
            "SELECT draft_id FROM drafts_fts WHERE drafts_fts MATCH ?",
            (_fts_escape("STATE-OF-THE-ART", "en-US"),),
        ).fetchone()
        assert row is not None and row["draft_id"] == "draft_en"
        assert conn.execute(
            "SELECT draft_id FROM drafts_fts WHERE drafts_fts MATCH ?",
            (_fts_escape("Mara", "en-US"),),
        ).fetchone()["draft_id"] == "draft_en"
    finally:
        conn.close()


def test_english_fts_normalizes_common_inflections_on_both_paths():
    from novelforge.app.api.memory import _fts_escape
    from novelforge.db.connection import index_draft_fts, set_meta

    conn = _db()
    try:
        set_meta(conn, "content_language", "en-US")
        index_draft_fts(
            conn, "draft_lemma", 1,
            "Mara was running north while she studied the old maps.",
        )
        for query in ("run", "runs", "study", "studies"):
            row = conn.execute(
                "SELECT draft_id FROM drafts_fts WHERE drafts_fts MATCH ?",
                (_fts_escape(query, "en-US"),),
            ).fetchone()
            assert row is not None and row["draft_id"] == "draft_lemma"

        conn.execute("DELETE FROM drafts_fts")
        index_draft_fts(conn, "draft_base", 2, "They run and study every dawn.")
        for query in ("running", "studied"):
            row = conn.execute(
                "SELECT draft_id FROM drafts_fts WHERE drafts_fts MATCH ?",
                (_fts_escape(query, "en-US"),),
            ).fetchone()
            assert row is not None and row["draft_id"] == "draft_base"
    finally:
        conn.close()


def test_split_manuscript_supports_english_words_roman_and_front_matter():
    from novelforge.coldstart import split_manuscript

    text = (
        "Prologue\nThe old war ended.\n\n"
        "Chapter One: Home\nMara woke before dawn.\n\n"
        "CHAPTER II — The Door\nThe sealed door opened.\n\n"
        "Epilogue\nShe finally returned."
    )

    chapters = split_manuscript(text)

    assert [chapter["chapter_no"] for chapter in chapters] == [1, 2, 3, 4]
    assert chapters[1]["title"] == "Chapter One: Home"
    assert "sealed door" in chapters[2]["text"]


def test_english_coldstart_dedup_is_casefolded_and_word_based():
    from novelforge.coldstart import dedup_proposals

    first = {
        "fact_type": "character_trait",
        "entity": "Mara",
        "new": {"predicate": "Appearance", "object": "Her eyes are bright blue"},
    }
    near = {
        "fact_type": "CHARACTER_TRAIT",
        "entity": "mara",
        "new": {"predicate": "appearance", "object": "Bright blue eyes"},
    }

    kept, dropped = dedup_proposals([first, near], language="en-US")

    assert kept == [first]
    assert dropped == 1


def test_english_coldstart_dedup_matches_existing_canon_case_insensitively():
    from novelforge.coldstart import dedup_proposals

    conn = _db()
    try:
        conn.execute(
            "INSERT INTO facts(id, fact_type, subject, predicate, object, status,"
            " valid_from_chapter, current_revision_id)"
            " VALUES('fact_en','character_trait','Mara','Role','Guild Warden',"
            " 'canon',1,'fact_en')"
        )
        proposal = {
            "fact_type": "character_trait", "entity": "mara",
            "new": {"predicate": "role", "object": "guild warden"},
        }

        kept, dropped = dedup_proposals(
            [proposal], conn, language="en-US",
        )

        assert kept == [] and dropped == 1
    finally:
        conn.close()


def test_cold_extract_uses_locked_english_profile_in_system_and_user_prompt():
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway
    from novelforge.control_plane.skill_base import SkillContext
    from novelforge.skills.cold_extract_skill import ColdExtractSkill
    from novelforge.writing_profiles import normalize_writing_selection

    provider = FakeProvider(responses=["```proposals\n[]\n```"])
    ledger = BudgetLedger()
    workspace = {
        "source_text": "Mara gave Rowan the key and told him the truth.",
        "chapter_no": 2,
        "writing_profile": normalize_writing_selection({
            "content_language": "en-US",
        }),
    }
    ctx = SkillContext(
        project_id="p_en", target_chapter=2, mode="human_gate",
        as_of_chapter=1, budget=ledger,
        llm=LLMGateway(provider, ledger), conn=None, workspace=workspace,
    )

    ColdExtractSkill().run(ctx)

    call = provider.calls[0]
    assert "English-language fiction chapter" in call["system"]
    assert "natural American English" in call["system"]
    assert call["messages"][0].content.startswith("Source text for Chapter 2")
    assert len(provider.calls) == 1
    assert workspace["proposals"] == []
    assert "degraded" not in workspace


def test_english_cold_extract_preserves_source_grounded_cjk_proper_noun():
    from novelforge.skills.cold_extract_skill import _parse_proposals_validated

    source = "Mara reached 青云山 before dusk and marked it on her map."
    raw = """```proposals
[{"op":"add","fact_type":"location","entity":"青云山",
  "new":{"subject":"青云山","predicate":"status","object":"Mapped by Mara"},
  "evidence_refs":[{"quote":"青云山"}]}]
```"""

    proposals = _parse_proposals_validated(
        raw, 2, content_language="en-US", source_text=source,
    )

    assert proposals and proposals[0]["entity"] == "青云山"


def test_cold_extract_retries_wrong_language_values_for_english_project():
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway
    from novelforge.control_plane.skill_base import SkillContext
    from novelforge.skills.cold_extract_skill import ColdExtractSkill
    from novelforge.writing_profiles import normalize_writing_selection

    wrong_language = [{
        "op": "add",
        "fact_type": "relationship",
        "entity": "Mara",
        "new": {
            "subject": "Mara",
            "predicate": "relationship",
            "object": "Rowan's trusted ally",
        },
        "evidence_refs": [{"chapter": 2, "quote": "她把钥匙交给了罗文"}],
    }]
    english = [{
        "op": "add",
        "fact_type": "relationship",
        "entity": "Mara",
        "new": {
            "subject": "Mara",
            "predicate": "relationship",
            "object": "Rowan's trusted ally",
        },
        "evidence_refs": [{"chapter": 2, "quote": "Mara gave Rowan the key"}],
    }]
    provider = FakeProvider(responses=[
        f"```proposals\n{json.dumps(wrong_language, ensure_ascii=False)}\n```",
        f"```proposals\n{json.dumps(english)}\n```",
    ])
    ledger = BudgetLedger()
    workspace = {
        "source_text": "Mara gave Rowan the key and told him the truth.",
        "chapter_no": 2,
        "writing_profile": normalize_writing_selection({
            "content_language": "en-US",
        }),
    }
    ctx = SkillContext(
        project_id="p_en", target_chapter=2, mode="human_gate",
        as_of_chapter=1, budget=ledger,
        llm=LLMGateway(provider, ledger), conn=None, workspace=workspace,
    )

    result = ColdExtractSkill().run(ctx)

    assert len(provider.calls) == 2
    assert workspace["cold_extract_escalated"] is True
    assert workspace["cold_extract_tier_used"] == "strong"
    assert workspace["proposals"] == result.payload["proposals"]
    assert workspace["proposals"][0]["new"]["object"] == "Rowan's trusted ally"
    assert "degraded" not in workspace


def test_cold_extract_degrades_to_empty_after_all_english_values_fail():
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway
    from novelforge.control_plane.skill_base import SkillContext
    from novelforge.skills.cold_extract_skill import ColdExtractSkill
    from novelforge.writing_profiles import normalize_writing_selection

    chinese = [{
        "op": "add",
        "fact_type": "knowledge",
        "entity": "玛拉",
        "new": {
            "subject": "玛拉",
            "predicate": "知道",
            "object": "密门的名字",
        },
        "evidence": "玛拉说出了密门的名字",
    }]
    response = f"```proposals\n{json.dumps(chinese, ensure_ascii=False)}\n```"
    provider = FakeProvider(responses=[response, response])
    ledger = BudgetLedger()
    workspace = {
        "source_text": "Mara spoke the sealed door's true name.",
        "chapter_no": 3,
        "writing_profile": normalize_writing_selection({
            "content_language": "en-US",
        }),
    }
    ctx = SkillContext(
        project_id="p_en", target_chapter=3, mode="human_gate",
        as_of_chapter=2, budget=ledger,
        llm=LLMGateway(provider, ledger), conn=None, workspace=workspace,
    )

    result = ColdExtractSkill().run(ctx)

    assert len(provider.calls) == 2
    assert workspace["proposals"] == []
    assert result.payload["proposals"] == []
    assert workspace["degraded"] is True
    assert workspace["fallback_reason"] == (
        "english_proposal_parse_or_language_validation_failed"
    )
    assert workspace["cold_extract_tier_used"] == "strong"
    assert workspace["raw_response"] == response


def test_style_anchor_selection_isolates_language_rights_and_profile_tags():
    from novelforge.craft.style_anchor import pick_style_anchors, render_anchor_block

    conn = _db()
    try:
        rows = (
            ("en_match", "en-US", 1, '["mystery"]'),
            ("en_general", "en-US", 1, "[]"),
            ("en_wrong_tag", "en-US", 1, '["romance"]'),
            ("en_unlicensed", "en-US", 0, '["mystery"]'),
            ("zh_anchor", "zh-CN", 1, '["mystery"]'),
        )
        for anchor_id, language, rights, tags in rows:
            conn.execute(
                "INSERT INTO style_anchors("
                "id, emotion, content, language, source_type, rights_confirmed,"
                " profile_tags) VALUES(?,?,?,?,?,?,?)",
                (
                    anchor_id, "suspense", "A licensed reference passage. " * 5,
                    language, "licensed", rights, tags,
                ),
            )
        conn.commit()

        picked = pick_style_anchors(
            conn, "suspense", 10,
            language="en-US", profile_tags=["mystery"],
        )

        assert {item["id"] for item in picked} == {"en_match", "en_general"}
        block = render_anchor_block(picked, language="en-US")
        assert "Authorized style references" in block
        assert "never copy wording" in block
        assert "文风参考" not in block
    finally:
        conn.close()
