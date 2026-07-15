"""Focused regressions for the plan-driven draft/alignment closed loop."""
from __future__ import annotations

import pytest

from novelforge.control_plane.budget import BudgetLedger
from novelforge.control_plane.llm.fake_provider import FakeProvider
from novelforge.control_plane.llm.gateway import LLMGateway
from novelforge.control_plane.orchestrator import (
    DraftRevisionConflict,
    _persist_draft,
    validate_final_candidate,
)
from novelforge.control_plane.skill_base import SkillContext
from novelforge.craft.candidate_judge import select_best
from novelforge.planning.actuals import extract_chapter_actuals, persist_chapter_actuals
from novelforge.planning.context import PlanContext
from novelforge.planning.scoring import (
    persist_alignment_report,
    recommend_plan_action,
    score_plan_alignment,
)
from novelforge.skills.chapter_draft_skill import ChapterDraftSkill


def _context() -> PlanContext:
    return PlanContext(
        target_chapter=21,
        story_plan={
            "title": "家族真相",
            "premise": "力量会考验人的选择",
            "ending_direction": "主角拒绝成为新的独裁者",
        },
        current_volume={
            "title": "宗门暗线",
            "objective": "锁定第一条幕后线索",
            "key_results": [{"id": "kr-evidence", "text": "取得皇室印记", "status": "pending"}],
        },
        current_contract={
            "chapter": 21,
            "goal": "找到被修改的族谱",
            "must_advance": ["发现宗门与家族的直接关联"],
            "must_include_entities": ["守阁长老"],
            "must_not_reveal": ["真正幕后黑手"],
            "arc_targets": ["arc-family"],
            "volume_kr_targets": ["kr-evidence"],
            "success_criteria": ["章末产生继续调查的动力"],
            "beats": [
                {
                    "seq": 1,
                    "beat_type": "setup",
                    "summary": "找到被修改的族谱",
                    "constraint_level": "hard",
                },
                {
                    "seq": 2,
                    "beat_type": "hook",
                    "summary": "决定继续调查",
                    "constraint_level": "soft",
                },
            ],
        },
        active_arcs=[{
            "id": "arc-family",
            "title": "家族覆灭真相",
            "objective": "确认宗门内部有人知情",
        }],
    )


def test_contract_renderer_and_draft_prompt_have_stable_order(tmp_path):
    contract = _context().render_contract_for_draft()
    assert "力量会考验人的选择" in contract
    assert "主角拒绝成为新的独裁者" in contract
    assert "锁定第一条幕后线索" in contract
    assert "找到被修改的族谱" in contract
    assert "真正幕后黑手" in contract
    assert "家族覆灭真相" in contract

    provider = FakeProvider(responses=["```draft\n" + "正文" * 600 + "\n```"])
    gateway = LLMGateway(provider, BudgetLedger(max_tokens=100_000, max_usd=10.0))
    from novelforge.db.connection import init_db

    conn = init_db(tmp_path / "prompt.db")
    ctx = SkillContext(
        project_id="p",
        target_chapter=21,
        mode="human_gate",
        as_of_chapter=20,
        budget=gateway.ledger,
        llm=gateway,
        conn=conn,
        workspace={
            "stable_context": "## 世界设定（稳定）\n稳定设定",
            "dynamic_context": "当前状态",
            "chapter_contract_context": "## 本章契约\n契约内容",
            "style_anchor_block": "## 文风锚点\n文风内容",
            "beats": _context().current_contract["beats"],
        },
        extra={"draft_target_chars": 1200},
    )
    result = ChapterDraftSkill().run(ctx)
    assert result.ok
    assert ctx.workspace["proposals"] == []
    assert result.dod_outcomes[-1].name == "proposal_output_valid"
    message = provider.calls[0]["messages"][-1].content
    assert (
        message.index("稳定设定")
        < message.index("当前状态")
        < message.index("契约内容")
        < message.index("文风内容")
        < message.index("## Beat Sheet")
    )
    conn.close()


def test_candidate_plan_block_precedes_literary_judge():
    context = _context()
    unsafe = (
        "找到被修改的族谱，发现宗门与家族的直接关联，守阁长老在场，"
        "章末产生继续调查的动力。真正幕后黑手终于现身。" + "甲" * 1200
    )
    safe = (
        "找到被修改的族谱，发现宗门与家族的直接关联，守阁长老在场，"
        "章末产生继续调查的动力，主角决定继续调查。" + "乙" * 1200
    )
    report = select_best(
        [
            {"draft_text": unsafe, "proposals": []},
            {"draft_text": safe, "proposals": []},
        ],
        chapter_contract=context.current_contract,
        plan_context=context,
    )
    assert report["winner"] == 1
    assert report["reason"] == "plan_block_veto"
    assert report["plan_blocks"] == [1, 0]
    assert report["contract_scores"][1] > 0
    assert report["arc_progress_scores"][1] >= 0


def test_actuals_alignment_persistence_supersedes_current_rows(tmp_path):
    from novelforge.db.connection import init_db

    conn = init_db(tmp_path / "alignment.db")
    conn.executemany(
        "INSERT INTO draft_index(id,chapter,revision_round,file_path,sha256,status)"
        " VALUES(?,?,?,?,?,'draft')",
        [
            ("draft-a", 21, 0, "l0/a.txt", "sha-a"),
            ("draft-b", 21, 1, "l0/b.txt", "sha-b"),
        ],
    )
    context = _context()
    text_a = "守阁长老帮他找到被修改的族谱。他发现宗门与家族的直接关联，章末产生继续调查的动力。"
    extraction_a = extract_chapter_actuals(
        21, text_a, context, draft_id="draft-a", seed_proposals=[]
    )
    assert extraction_a.proposals == []
    assert extraction_a.actuals.events
    actuals_a = persist_chapter_actuals(conn, extraction_a.actuals)
    report_a = score_plan_alignment(
        extraction_a.actuals, context, draft_text=text_a, actuals_id=actuals_a
    )
    report_id_a = persist_alignment_report(conn, report_a)
    conn.commit()

    text_b = text_a + "真正幕后黑手在门后现身。"
    extraction_b = extract_chapter_actuals(
        21, text_b, context, draft_id="draft-b", seed_proposals=[]
    )
    actuals_b = persist_chapter_actuals(conn, extraction_b.actuals)
    report_b = score_plan_alignment(
        extraction_b.actuals,
        context,
        draft_text=text_b,
        interaction_mode="autonomous",
        actuals_id=actuals_b,
    )
    report_id_b = persist_alignment_report(conn, report_b)
    conn.commit()

    assert actuals_a and actuals_b and actuals_a != actuals_b
    assert report_id_a and report_id_b and report_id_a != report_id_b
    assert report_b.reveal_score == 0.0
    assert report_b.decision == "pause"
    assert any(item.evidence_spans for item in report_b.findings if item.category == "reveal")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM chapter_actuals WHERE chapter=21 AND status='current'"
    ).fetchone()["n"] == 1
    assert conn.execute(
        "SELECT status FROM chapter_actuals WHERE id=?", (actuals_a,)
    ).fetchone()["status"] == "superseded"
    assert conn.execute(
        "SELECT status FROM plan_alignment_reports WHERE id=?", (report_id_a,)
    ).fetchone()["status"] == "superseded"
    conn.close()


def test_actuals_extract_state_change_and_non_forbidden_reveal_evidence():
    text = "守阁长老告诉主角：宗门曾收留过他的父亲。"
    proposal = {
        "op": "add",
        "fact_type": "knowledge",
        "entity": "主角",
        "new": {
            "subject": "主角",
            "predicate": "得知",
            "object": "宗门曾收留过他的父亲",
        },
        "evidence_refs": [{"chapter": 21, "quote": "宗门曾收留过他的父亲"}],
    }
    extraction = extract_chapter_actuals(
        21, text, _context(), seed_proposals=[proposal]
    )
    assert extraction.actuals.state_changes[0].after == "宗门曾收留过他的父亲"
    assert extraction.actuals.state_changes[0].evidence_spans
    reveal = next(
        item for item in extraction.actuals.revealed_information
        if item.information == "宗门曾收留过他的父亲"
    )
    assert not reveal.forbidden
    assert reveal.evidence_spans[0].text == "宗门曾收留过他的父亲"


def test_mode_specific_recommendations():
    assert recommend_plan_action(0.18, interaction_mode="guided") == "patch"
    assert recommend_plan_action(0.18, interaction_mode="autonomous") == "keep"
    assert recommend_plan_action(0.35, interaction_mode="autonomous") == "replan"
    assert recommend_plan_action(0.41, interaction_mode="autonomous") == "pause"
    assert recommend_plan_action(0.01, hard_reveal=True) == "pause"


def test_persist_draft_executes_plan_version_and_enforces_locked_cas(tmp_path):
    from novelforge.db.connection import init_db

    conn = init_db(tmp_path / "draft-cas.db")
    conn.execute(
        "INSERT INTO chapter_plan_versions(id,chapter,version,payload_json,status,activated_at)"
        " VALUES('cpv-1',1,1,'{}','active',datetime('now'))"
    )
    conn.execute(
        "INSERT INTO chapter_cards(id,chapter,title,goal,status,current_plan_version_id)"
        " VALUES('card-1',1,'第一章','开局','planned','cpv-1')"
    )
    conn.commit()

    draft_id = _persist_draft(conn, "正文" * 600, 1, "p", str(tmp_path / "draft-cas.db"))
    assert draft_id
    assert conn.execute(
        "SELECT status FROM chapter_plan_versions WHERE id='cpv-1'"
    ).fetchone()["status"] == "executed"
    assert conn.execute(
        "SELECT status FROM chapter_cards WHERE chapter=1"
    ).fetchone()["status"] == "drafted"

    with pytest.raises(DraftRevisionConflict) as exc:
        _persist_draft(
            conn,
            "另一稿" * 600,
            1,
            "p",
            str(tmp_path / "draft-cas.db"),
            expected_base_revision_id="stale-draft",
        )
    assert exc.value.current_id == draft_id
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM draft_index WHERE chapter=1"
    ).fetchone()["n"] == 1

    revised_id = _persist_draft(
        conn,
        "作者修订" * 600,
        1,
        "p",
        str(tmp_path / "draft-cas.db"),
        expected_base_revision_id=draft_id,
    )
    card = conn.execute(
        "SELECT draft_id, word_count, status FROM chapter_cards WHERE chapter=1"
    ).fetchone()
    assert card["draft_id"] == revised_id
    assert card["word_count"] == len("作者修订" * 600)
    assert card["status"] == "drafted"
    conn.close()


def test_manual_candidate_validation_order_and_shape(tmp_path):
    from novelforge.db.connection import init_db

    class RecordingRegistry:
        def __init__(self):
            self.calls: list[str] = []

        def invoke(self, name, ctx):
            self.calls.append(name)
            if name == "continuity_check":
                ctx.workspace["continuity_issues"] = []
            elif name == "craft_check":
                ctx.workspace["craft_issues"] = []
            return None

    conn = init_db(tmp_path / "manual-validation.db")
    registry = RecordingRegistry()
    ctx = SkillContext(
        project_id="p",
        target_chapter=21,
        mode="human_gate",
        as_of_chapter=20,
        budget=None,
        llm=None,
        conn=conn,
        workspace={"world_state": object(), "beats": _context().selected_beats},
    )
    result = validate_final_candidate(
        ctx,
        "找到被修改的族谱。" + "正文" * 600,
        plan_context=_context(),
        proposals=[{"op": "add", "new": {}}],
        registry=registry,
        score_quality=False,
    )
    assert registry.calls == ["continuity_check", "craft_check"]
    assert result["ok"]
    assert result["hard_blocks"] == []
    assert result["quality"] is None
    assert "plan_evaluation" in result
    assert result["proposals"]
    conn.close()
