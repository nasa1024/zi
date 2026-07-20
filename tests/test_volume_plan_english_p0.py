from __future__ import annotations

import json
import re
import sqlite3


def _english_plan(chapter: int) -> dict:
    return {
        "chapter": chapter,
        "title": f"Chapter {chapter}: The Sealed Door",
        "goal": "Mara must choose whether to open the sealed door before her rival arrives.",
        "arc_targets": [],
        "volume_kr_targets": [],
        "milestone_targets": [],
        "must_advance": ["Mara makes an observable choice about the sealed door."],
        "must_resolve": ["Resolve whether Mara accepts the immediate risk."],
        "must_include_entities": ["Mara"],
        "must_not_reveal": ["Do not reveal who sent the warning letter."],
        "must_not_resolve": ["Do not settle the larger mystery behind the tower."],
        "forbidden_state_changes": ["The rival cannot become Mara's ally yet."],
        "target_state": {
            "door status": "The sealed door is open and Mara has accepted the consequence."
        },
        "success_criteria": [
            "The prose shows Mara's choice, the door opening, and the immediate cost."
        ],
        "hook_text": "A familiar voice answers from the darkness beyond the door.",
        "target_emotion": "uneasy resolve",
        "opening_hook_type": "suspense",
        "hook_type": "mystery",
        "expectation_score": 4,
        "beats": [
            {
                "beat_type": "setup",
                "summary": "Mara reaches the sealed door as her rival closes in.",
                "value_axis": "uncertainty to pressure",
                "arc_id": None,
                "constraint_level": "hard",
            },
            {
                "beat_type": "tension_point",
                "summary": "The warning letter makes delay more dangerous than action.",
                "value_axis": "pressure to urgency",
                "arc_id": None,
                "constraint_level": "soft",
            },
            {
                "beat_type": "turn",
                "summary": "Mara opens the door and accepts the immediate cost.",
                "value_axis": "urgency to consequence",
                "arc_id": None,
                "constraint_level": "soft",
            },
            {
                "beat_type": "hook",
                "summary": "A hidden speaker recognizes Mara by name.",
                "value_axis": "consequence to anticipation",
                "arc_id": None,
                "constraint_level": "hard",
            },
        ],
    }


def _plans_response(plans: list[dict]) -> str:
    return f"```plans\n{json.dumps(plans, ensure_ascii=False)}\n```"


def _skill_context(responses: list[str], *, plan_from: int = 1, plan_to: int = 2):
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway
    from novelforge.control_plane.skill_base import SkillContext
    from novelforge.writing_profiles import normalize_writing_selection

    provider = FakeProvider(responses=responses)
    ledger = BudgetLedger(max_tokens=1_000_000, max_usd=10.0)
    workspace = {
        "volume_brief": {
            "volume_no": 1,
            "title": "The Glass Stair",
            "synopsis": "Mara enters the tower to recover her stolen name.",
            "rolling_summary": "Mara has found the first locked passage.",
        },
        "plan_from": plan_from,
        "plan_to": plan_to,
        "foreshadow_due": [{"label": "The warning letter", "due_chapter": plan_to}],
        "planning_hints": "Keep the choice costly and specific.",
        "writing_profile": normalize_writing_selection({"content_language": "en-US"}),
    }
    context = SkillContext(
        project_id="project_en",
        target_chapter=plan_from,
        mode="human_gate",
        as_of_chapter=plan_from - 1,
        budget=ledger,
        llm=LLMGateway(provider, ledger),
        conn=None,
        workspace=workspace,
    )
    return context, provider


def test_volume_plan_retries_when_any_nested_visible_value_is_not_english():
    from novelforge.skills.volume_plan_skill import VolumePlanSkill

    wrong = [_english_plan(1), _english_plan(2)]
    wrong[1]["beats"][1]["summary"] = "危机继续加剧"
    valid = [_english_plan(1), _english_plan(2)]
    context, provider = _skill_context([
        _plans_response(wrong),
        _plans_response(valid),
    ])

    result = VolumePlanSkill().run(context)

    assert result.ok is True
    assert len(provider.calls) == 2
    assert [plan["chapter"] for plan in result.payload["chapter_plans"]] == [1, 2]
    assert context.workspace["volume_plan_degraded"] is False
    assert "Volume So Far" in provider.calls[0]["messages"][0].content
    assert "Foreshadowing Due" in provider.calls[0]["messages"][0].content
    assert "Every user-visible value" in provider.calls[0]["system"]


def test_strict_volume_parser_rejects_extra_chapters_outside_exact_window():
    from novelforge.skills.volume_plan_skill import _parse_plans_validated

    response = _plans_response([
        _english_plan(1), _english_plan(2), _english_plan(99),
    ])

    assert _parse_plans_validated(
        response, 1, 2, content_language="en-US",
    ) is None


def test_volume_plan_uses_complete_deterministic_english_fallback_after_retries():
    from novelforge.skills.volume_plan_skill import VolumePlanSkill

    context, provider = _skill_context(
        ["not json", "still not json"], plan_from=3, plan_to=5,
    )

    result = VolumePlanSkill().run(context)
    plans = result.payload["chapter_plans"]

    assert result.ok is True
    assert len(provider.calls) == 2
    assert [plan["chapter"] for plan in plans] == [3, 4, 5]
    assert all(3 <= len(plan["beats"]) <= 5 for plan in plans)
    assert all(plan["beats"][-1]["beat_type"] == "hook" for plan in plans)
    assert context.workspace["volume_plan_degraded"] is True
    assert context.workspace["volume_plan_fallback_reason"] == (
        "volume_plan_parse_window_or_language_validation_failed"
    )
    assert re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", json.dumps(plans)) is None


def test_english_volume_service_localizes_route_rationale_and_missing_key_error(
    monkeypatch,
):
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway
    from novelforge.db.connection import init_db_from_conn
    from novelforge.planning.service import PlanningService
    from novelforge.writing_profiles import initialize_project_writing_profile
    import novelforge.planning.service as service_module

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db_from_conn(conn)
    initialize_project_writing_profile(conn, {"content_language": "en-US"})
    conn.execute(
        "INSERT INTO volumes(id, volume_no, title, synopsis, objective, key_results, "
        "start_chapter, end_chapter) VALUES(?,?,?,?,?,?,?,?)",
        (
            "volume_1", 1, "The Glass Stair",
            "Mara climbs the tower to recover her stolen name.",
            "Mara opens the first sealed passage.", "[]", 1, 10,
        ),
    )
    conn.commit()

    provider = FakeProvider(responses=[
        _plans_response([_english_plan(1), _english_plan(2)]),
    ])
    ledger = BudgetLedger(max_tokens=1_000_000, max_usd=10.0)
    gateway = LLMGateway(provider, ledger)
    monkeypatch.setattr(
        service_module, "build_project_gateway", lambda *args, **kwargs: gateway,
    )

    window = PlanningService().generate_window(
        conn,
        project_id="project_en",
        start_chapter=1,
        horizon=2,
        source="volume_api",
        volume_no=1,
    )

    assert window.error is None
    assert window.options[0].title == "Volume 1 Chapter Plan"
    assert window.options[0].rationale == (
        "Generated by the unified planning service and applied as one transactional window."
    )
    assert window.metadata == {"degraded": False, "tier_used": "strong"}

    monkeypatch.setattr(
        service_module, "build_project_gateway", lambda *args, **kwargs: None,
    )
    missing_key = PlanningService().generate_window(
        conn,
        project_id="project_en",
        start_chapter=1,
        horizon=2,
        source="volume_api",
        volume_no=1,
    )
    assert missing_key.error == (
        "The creative model is unavailable because no API key is configured."
    )
    assert re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", missing_key.error) is None
    conn.close()
