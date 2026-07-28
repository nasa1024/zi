"""Unified planning service/API acceptance tests (offline and deterministic)."""
from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from novelforge.planning.models import DecisionOptionDraft
from novelforge.planning.service import GeneratedPlanWindow, PlanningService


def _chapter(chapter: int, marker: str) -> dict:
    return {
        "chapter": chapter,
        "title": f"{marker}-第{chapter}章",
        "goal": f"推进 {marker}",
        "arc_targets": [f"arc-{marker}"],
        "volume_kr_targets": [f"volume-1:kr-{marker}"],
        "milestone_targets": [f"milestone-{marker}"],
        "must_advance": [f"advance-{marker}"],
        "must_resolve": [],
        "must_include_entities": ["protagonist"],
        "must_not_reveal": [f"secret-{marker}"],
        "must_not_resolve": [f"final-{marker}"],
        "forbidden_state_changes": [f"forbidden-{marker}"],
        "target_state": {"route": marker, "chapter": chapter},
        "success_criteria": [f"正文出现 {marker} 的推进证据"],
        "hook_text": f"{marker} 留下新问题",
        "target_emotion": "紧张",
        "opening_hook_type": "suspense",
        "hook_type": "promise",
        "expectation_score": 4,
        "beats": [
            {
                "seq": 1,
                "beat_type": "setup",
                "summary": f"{marker}-hard",
                "value_axis": "目标清晰↑",
                "arc_id": None,
                "constraint_level": "hard",
            },
            {
                "seq": 2,
                "beat_type": "hook",
                "summary": f"{marker}-hook",
                "value_axis": "期待↑",
                "arc_id": None,
                "constraint_level": "soft",
            },
        ],
    }


def _window(
    project_id: str,
    start_chapter: int,
    horizon: int,
    marker: str,
    *,
    source: str = "test",
    candidate_markers: list[str] | None = None,
) -> GeneratedPlanWindow:
    markers = candidate_markers or [marker]
    options = []
    for option_marker in markers:
        chapters = [
            _chapter(start_chapter + offset, option_marker)
            for offset in range(horizon)
        ]
        options.append(DecisionOptionDraft(
            title=f"路线-{option_marker}",
            summary=f"{option_marker} 摘要",
            payload={
                "route_title": f"路线-{option_marker}",
                "route_summary": f"{option_marker} 摘要",
                "chapters": chapters,
            },
            score=8.0,
        ))
    return GeneratedPlanWindow(
        project_id=project_id,
        start_chapter=start_chapter,
        horizon=horizon,
        source=source,
        options=options,
        metadata={"fake": True},
    )


@pytest.fixture
def conn():
    from novelforge.db.connection import init_db_from_conn

    value = sqlite3.connect(":memory:")
    value.row_factory = sqlite3.Row
    value.execute("PRAGMA foreign_keys=ON")
    init_db_from_conn(value)
    yield value
    value.close()


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_DATA", str(tmp_path))
    monkeypatch.setenv("NOVELFORGE_PROVIDER", "fake")
    monkeypatch.delenv("NOVELFORGE_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

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
    response = client.post(
        "/v1/projects", json={"name": "规划闭环测试", "genre": "xuanhuan"}
    )
    assert response.status_code == 201, response.text
    return response.json()["project_id"]


def _open_project(project_id: str):
    from novelforge.app.deps import get_registry

    return get_registry().open_conn(project_id)


def _fake_generate(marker_factory):
    def generate(self, conn, **kwargs):
        marker = marker_factory(kwargs) if callable(marker_factory) else marker_factory
        return _window(
            kwargs["project_id"],
            kwargs["start_chapter"],
            kwargs["horizon"],
            marker,
            source=kwargs["source"],
            candidate_markers=kwargs.get("_candidate_markers"),
        )

    return generate


class TestPlanningServiceTransactions:
    def test_apply_window_rolls_back_the_entire_window(self, conn):
        conn.executescript("""
            CREATE TRIGGER fail_second_chapter
            BEFORE INSERT ON chapter_cards
            WHEN NEW.chapter=2
            BEGIN
                SELECT RAISE(ABORT, 'forced second chapter failure');
            END;
        """)
        conn.commit()
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO meta_kv(key,value) VALUES('outer-sentinel','kept')"
        )

        with pytest.raises(sqlite3.IntegrityError, match="forced second chapter failure"):
            PlanningService().apply_window(
                conn,
                window=_window("project", 1, 2, "atomic"),
                decision_id="decision-atomic",
                lock=True,
                replace_policy="replace_unwritten",
            )

        # The service savepoint removes every planning write while preserving
        # the caller-owned outer transaction.
        assert conn.execute(
            "SELECT value FROM meta_kv WHERE key='outer-sentinel'"
        ).fetchone()["value"] == "kept"
        for table in (
            "chapter_cards",
            "beats",
            "chapter_plan_versions",
            "chapter_plan_version_beats",
        ):
            assert conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0
        conn.commit()

    def test_replacement_is_append_only_and_keeps_version_beats(self, conn):
        service = PlanningService()
        first = service.apply_window(
            conn,
            window=_window("project", 5, 1, "old"),
            decision_id="decision-old",
            lock=False,
            replace_policy="replace_unwritten",
        )
        second = service.apply_window(
            conn,
            window=_window("project", 5, 1, "new"),
            decision_id="decision-new",
            lock=True,
            replace_policy="replace_unwritten",
        )

        assert first.version_ids[5] != second.version_ids[5]
        versions = conn.execute(
            "SELECT id,version,status,source_decision_id,payload_json"
            " FROM chapter_plan_versions WHERE chapter=5 ORDER BY version"
        ).fetchall()
        assert [(row["version"], row["status"]) for row in versions] == [
            (1, "superseded"),
            (2, "active"),
        ]
        assert [row["source_decision_id"] for row in versions] == [
            "decision-old",
            "decision-new",
        ]
        assert json.loads(versions[0]["payload_json"])["target_state"]["route"] == "old"
        assert json.loads(versions[1]["payload_json"])["target_state"]["route"] == "new"

        version_beats = conn.execute(
            "SELECT v.version,b.seq,b.summary,b.constraint_level"
            " FROM chapter_plan_version_beats b"
            " JOIN chapter_plan_versions v ON v.id=b.plan_version_id"
            " WHERE v.chapter=5 ORDER BY v.version,b.seq"
        ).fetchall()
        assert [(row["version"], row["summary"]) for row in version_beats] == [
            (1, "old-hard"),
            (1, "old-hook"),
            (2, "new-hard"),
            (2, "new-hook"),
        ]
        card = conn.execute(
            "SELECT plan_version,locked,source_decision_id,current_plan_version_id"
            " FROM chapter_cards WHERE chapter=5"
        ).fetchone()
        assert dict(card) == {
            "plan_version": 2,
            "locked": 1,
            "source_decision_id": "decision-new",
            "current_plan_version_id": second.version_ids[5],
        }

    def test_service_and_agent_entry_share_storage_semantics(self, conn):
        from novelforge.planning.repository import apply_chapter_route

        PlanningService().apply_window(
            conn,
            window=_window("project", 10, 1, "service"),
            decision_id="decision-service",
            lock=True,
            replace_policy="replace_unwritten",
        )
        agent_payload = _window("project", 11, 1, "agent").options[0].payload
        planned = apply_chapter_route(conn, {
            "decision_id": "decision-agent",
            "selected_option_id": "option-agent",
            "options": [{
                "id": "option-agent",
                "title": "Agent 路线",
                "summary": "Agent 入口",
                "payload": agent_payload,
            }],
        })
        assert planned == [11]

        cards = conn.execute(
            "SELECT chapter,plan_version,locked,source_decision_id,current_plan_version_id"
            " FROM chapter_cards WHERE chapter IN (10,11) ORDER BY chapter"
        ).fetchall()
        assert [(r["chapter"], r["plan_version"], r["locked"]) for r in cards] == [
            (10, 1, 1),
            (11, 1, 1),
        ]
        assert [r["source_decision_id"] for r in cards] == [
            "decision-service",
            "decision-agent",
        ]
        assert all(r["current_plan_version_id"] for r in cards)
        constraints = conn.execute(
            "SELECT chapter,seq,constraint_level FROM beats"
            " WHERE chapter IN (10,11) ORDER BY chapter,seq"
        ).fetchall()
        assert [(r["chapter"], r["seq"], r["constraint_level"]) for r in constraints] == [
            (10, 1, "hard"),
            (10, 2, "soft"),
            (11, 1, "hard"),
            (11, 2, "soft"),
        ]


class TestPlanningHttpWorkflow:
    def test_generate_approve_contract_and_plan_versions(
        self, client, project, monkeypatch,
    ):
        def generated(self, conn, **kwargs):
            return _window(
                kwargs["project_id"],
                kwargs["start_chapter"],
                kwargs["horizon"],
                "ignored",
                source=kwargs["source"],
                candidate_markers=["candidate-a", "candidate-b"],
            )

        monkeypatch.setattr(PlanningService, "generate_window", generated)
        response = client.post(
            f"/v1/{project}/planning/windows/generate",
            json={"start_chapter": 21, "horizon": 2, "candidate_count": 2},
        )
        assert response.status_code == 201, response.text
        generated_body = response.json()
        decision = generated_body["decision"]
        assert decision["status"] == "awaiting_choice"
        assert len(decision["options"]) == 2

        conn = _open_project(project)
        try:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM chapter_cards"
            ).fetchone()["n"] == 0
        finally:
            conn.close()

        selected_id = decision["options"][1]["id"]
        approved = client.post(
            f"/v1/{project}/planning/windows/{decision['decision_id']}/approve",
            json={
                "option_id": selected_id,
                "lock": True,
                "replace_policy": "replace_unwritten",
            },
        )
        assert approved.status_code == 200, approved.text
        approved_body = approved.json()
        assert approved_body["planned"] == [21, 22]
        assert approved_body["selected_option_id"] == selected_id
        assert set(approved_body["version_ids"]) == {"21", "22"}

        contract = client.get(f"/v1/{project}/chapters/21/contract")
        assert contract.status_code == 200, contract.text
        contract_body = contract.json()
        assert contract_body["contract"]["target_state"]["route"] == "candidate-b"
        assert contract_body["contract"]["must_advance"] == ["advance-candidate-b"]
        assert bool(contract_body["contract"]["locked"]) is True
        assert contract_body["plan_version"]["status"] == "active"
        assert contract_body["plan_version"]["source_decision_id"] == decision["decision_id"]

        edited = client.patch(
            f"/v1/{project}/chapter-cards/21",
            json={"goal": "作者人工修订目标"},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["plan_version"] == 2

        versions = client.get(f"/v1/{project}/chapters/21/plan-versions")
        assert versions.status_code == 200
        assert [(v["version"], v["status"]) for v in versions.json()] == [
            (2, "active"),
            (1, "superseded"),
        ]
        assert versions.json()[0]["payload"]["goal"] == "作者人工修订目标"
        assert versions.json()[1]["payload"]["target_state"]["route"] == "candidate-b"
        assert [beat["constraint_level"] for beat in versions.json()[0]["beats"]] == [
            "hard",
            "soft",
        ]
        assert len(versions.json()[1]["beats"]) == 2

    def test_cross_project_planning_decision_is_not_a_bearer_capability(
        self, client, project, monkeypatch,
    ):
        monkeypatch.setattr(
            PlanningService,
            "generate_window",
            _fake_generate("project-a"),
        )
        generated = client.post(
            f"/v1/{project}/planning/windows/generate",
            json={"start_chapter": 1, "horizon": 1},
        )
        assert generated.status_code == 201, generated.text
        decision_id = generated.json()["decision"]["decision_id"]

        other = client.post(
            "/v1/projects", json={"name": "另一个项目", "genre": "xuanhuan"}
        )
        assert other.status_code == 201, other.text
        other_project = other.json()["project_id"]

        response = client.post(
            f"/v1/{other_project}/planning/windows/{decision_id}/approve",
            json={"replace_policy": "replace_unwritten"},
        )
        assert response.status_code == 404, response.text

        conn = _open_project(project)
        try:
            decision = conn.execute(
                "SELECT status,selected_option_id FROM decision_sets WHERE id=?",
                (decision_id,),
            ).fetchone()
            assert dict(decision) == {
                "status": "awaiting_choice",
                "selected_option_id": None,
            }
        finally:
            conn.close()

    def test_failed_window_apply_rolls_back_decision_resolution(
        self, client, project, monkeypatch,
    ):
        monkeypatch.setattr(
            PlanningService,
            "generate_window",
            _fake_generate("approval-rollback"),
        )
        generated = client.post(
            f"/v1/{project}/planning/windows/generate",
            json={"start_chapter": 1, "horizon": 2},
        )
        assert generated.status_code == 201, generated.text
        decision = generated.json()["decision"]

        conn = _open_project(project)
        try:
            conn.executescript("""
                CREATE TRIGGER fail_approval_second_chapter
                BEFORE INSERT ON chapter_cards
                WHEN NEW.chapter=2
                BEGIN
                    SELECT RAISE(ABORT, 'forced approval failure');
                END;
            """)
            conn.commit()
        finally:
            conn.close()

        from novelforge.app.api.planning import (
            ApproveWindowRequest,
            approve_planning_window,
        )
        from novelforge.app.deps import get_registry

        with pytest.raises(sqlite3.IntegrityError, match="forced approval failure"):
            approve_planning_window(
                project,
                decision["decision_id"],
                ApproveWindowRequest(replace_policy="replace_unwritten"),
                get_registry(),
            )

        conn = _open_project(project)
        try:
            durable = conn.execute(
                "SELECT status,selected_option_id,resolved_at FROM decision_sets WHERE id=?",
                (decision["decision_id"],),
            ).fetchone()
            assert dict(durable) == {
                "status": "awaiting_choice",
                "selected_option_id": None,
                "resolved_at": None,
            }
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM decision_options"
                " WHERE decision_id=? AND selected=1",
                (decision["decision_id"],),
            ).fetchone()["n"] == 0
            for table in (
                "chapter_cards",
                "beats",
                "chapter_plan_versions",
                "chapter_plan_version_beats",
            ):
                assert conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table}"
                ).fetchone()["n"] == 0
        finally:
            conn.close()

    def test_legacy_volume_api_uses_the_same_versioned_contract(
        self, client, project, monkeypatch,
    ):
        monkeypatch.setattr(
            PlanningService,
            "generate_window",
            _fake_generate("volume-entry"),
        )
        created = client.post(f"/v1/{project}/volumes", json={
            "volume_no": 1,
            "title": "统一入口卷",
            "start_chapter": 10,
            "end_chapter": 10,
        })
        assert created.status_code == 201
        response = client.post(
            f"/v1/{project}/volumes/1/plan",
            json={"from_chapter": 10, "to_chapter": 10},
        )
        assert response.status_code == 200, response.text
        card = response.json()["planned"][0]
        assert card["plan_version"] == 1
        assert card["locked"] is True
        assert card["source_decision_id"]
        assert card["current_plan_version_id"]
        assert [beat["constraint_level"] for beat in card["beats"]] == ["hard", "soft"]

    def test_failed_compatibility_apply_keeps_decision_awaiting(
        self, client, project, monkeypatch,
    ):
        monkeypatch.setattr(
            PlanningService,
            "generate_window",
            _fake_generate("compatibility-rollback"),
        )
        created = client.post(f"/v1/{project}/volumes", json={
            "volume_no": 1,
            "title": "兼容入口事务卷",
            "start_chapter": 10,
            "end_chapter": 11,
        })
        assert created.status_code == 201, created.text

        conn = _open_project(project)
        try:
            conn.executescript("""
                CREATE TRIGGER fail_compatibility_second_chapter
                BEFORE INSERT ON chapter_cards
                WHEN NEW.chapter=11
                BEGIN
                    SELECT RAISE(ABORT, 'forced compatibility failure');
                END;
            """)
            conn.commit()
        finally:
            conn.close()

        response = client.post(
            f"/v1/{project}/volumes/1/plan",
            json={"from_chapter": 10, "to_chapter": 11},
        )
        assert response.status_code == 200, response.text
        assert "forced compatibility failure" in response.json()["error"]

        conn = _open_project(project)
        try:
            decision = conn.execute(
                "SELECT id,status,selected_option_id,resolved_at FROM decision_sets"
                " WHERE stage='chapter_route' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            assert decision is not None
            assert dict(decision) == {
                "id": decision["id"],
                "status": "awaiting_choice",
                "selected_option_id": None,
                "resolved_at": None,
            }
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM decision_options"
                " WHERE decision_id=? AND selected=1",
                (decision["id"],),
            ).fetchone()["n"] == 0
            for table in (
                "chapter_cards",
                "beats",
                "chapter_plan_versions",
                "chapter_plan_version_beats",
            ):
                assert conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table}"
                ).fetchone()["n"] == 0
        finally:
            conn.close()

    def test_replan_preserves_locked_then_replace_unwritten_overrides_it(
        self, client, project, monkeypatch,
    ):
        conn = _open_project(project)
        try:
            service = PlanningService()
            service.apply_window(
                conn,
                window=_window(project, 2, 1, "locked-seed"),
                decision_id="seed-locked",
                lock=True,
                replace_policy="replace_unwritten",
            )
            service.apply_window(
                conn,
                window=_window(project, 3, 1, "open-seed"),
                decision_id="seed-open",
                lock=False,
                replace_policy="replace_unwritten",
            )
        finally:
            conn.close()

        calls = {"n": 0}

        def generated(self, conn, **kwargs):
            calls["n"] += 1
            return _window(
                kwargs["project_id"],
                kwargs["start_chapter"],
                kwargs["horizon"],
                f"replan-{calls['n']}",
                source=kwargs["source"],
            )

        monkeypatch.setattr(PlanningService, "generate_window", generated)

        preserve = client.post(f"/v1/{project}/chapters/1/replan", json={
            "mode": "patch",
            "horizon": 2,
            "preserve_locked": True,
            "reason": "接受新增角色",
            "apply": True,
        })
        assert preserve.status_code == 201, preserve.text
        assert preserve.json()["applied"]["planned"] == [3]
        assert preserve.json()["applied"]["skipped"] == [2]

        replace = client.post(f"/v1/{project}/chapters/1/replan", json={
            "mode": "replan",
            "horizon": 2,
            "preserve_locked": False,
            "reason": "显式替换全部未写章节",
            "apply": True,
        })
        assert replace.status_code == 201, replace.text
        assert replace.json()["applied"]["planned"] == [2, 3]
        assert replace.json()["applied"]["skipped"] == []

        conn = _open_project(project)
        try:
            states = conn.execute(
                "SELECT chapter,version,status,source_alignment_report_id"
                " FROM chapter_plan_versions WHERE chapter IN (2,3)"
                " ORDER BY chapter,version"
            ).fetchall()
        finally:
            conn.close()
        assert [(r["chapter"], r["version"], r["status"]) for r in states] == [
            (2, 1, "superseded"),
            (2, 2, "active"),
            (3, 1, "superseded"),
            (3, 2, "superseded"),
            (3, 3, "active"),
        ]

    def test_draft_update_uses_optimistic_revision_and_returns_409(
        self, client, project,
    ):
        from novelforge.app.deps import get_registry
        from novelforge.control_plane.orchestrator import _persist_draft

        registry = get_registry()
        project_meta = registry.get(project)
        conn = registry.open_conn(project)
        try:
            base_revision = _persist_draft(
                conn, "第一版正文", 1, project, project_meta.db_path
            )
        finally:
            conn.close()
        assert base_revision

        first_save = client.patch(f"/v1/{project}/chapters/1/draft", json={
            "draft_text": "第二版正文",
            "base_revision_id": base_revision,
        })
        assert first_save.status_code == 200, first_save.text
        assert first_save.json()["revision_round"] == 1
        assert first_save.json()["alignment_status"] == "stale"

        stale_save = client.patch(f"/v1/{project}/chapters/1/draft", json={
            "draft_text": "基于旧版的冲突保存",
            "base_revision_id": base_revision,
        })
        assert stale_save.status_code == 409, stale_save.text
        assert "刷新后再保存" in stale_save.json()["error"]["message"]

    def test_concurrent_draft_updates_accept_only_one_writer(
        self, project, monkeypatch,
    ):
        from novelforge.app.api.planning import DraftUpdateRequest, update_chapter_draft
        from novelforge.app.deps import get_registry
        import novelforge.control_plane.orchestrator as orchestrator_module

        registry = get_registry()
        project_meta = registry.get(project)
        conn = registry.open_conn(project)
        try:
            base_revision = orchestrator_module._persist_draft(
                conn, "第一版正文", 1, project, project_meta.db_path
            )
        finally:
            conn.close()
        assert base_revision

        original_persist = orchestrator_module._persist_draft
        both_passed_preflight = threading.Barrier(2)

        def synchronized_persist(*args, **kwargs):
            both_passed_preflight.wait(timeout=5)
            return original_persist(*args, **kwargs)

        monkeypatch.setattr(
            orchestrator_module, "_persist_draft", synchronized_persist
        )

        def save(text: str):
            try:
                result = update_chapter_draft(
                    project,
                    1,
                    DraftUpdateRequest(
                        draft_text=text,
                        base_revision_id=base_revision,
                    ),
                    registry,
                )
                return (200, result["revision_id"])
            except HTTPException as exc:
                return (exc.status_code, exc.detail)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ["并发修订 A", "并发修订 B"]))

        assert sorted(status for status, _ in results) == [200, 409]
        conn = registry.open_conn(project)
        try:
            revisions = conn.execute(
                "SELECT revision_round,id FROM draft_index"
                " WHERE chapter=1 ORDER BY revision_round"
            ).fetchall()
            assert [row["revision_round"] for row in revisions] == [0, 1]
            accepted = next(value for status, value in results if status == 200)
            assert revisions[-1]["id"] == accepted
        finally:
            conn.close()

    def test_concurrent_decision_selection_accepts_exactly_one_option(self, tmp_path):
        """Both requests may read a pending decision; only one may own it."""
        from novelforge.db.connection import init_db
        from novelforge.planning.repository import (
            DecisionConflictError,
            create_decision,
            resolve_decision,
        )

        db_path = tmp_path / "decisions.db"
        setup = init_db(db_path)
        try:
            setup.execute(
                "INSERT INTO sessions(id, client, actor) VALUES(?, 'api', 'test')",
                ("session-concurrent",),
            )
            decision = create_decision(
                setup,
                session_id="session-concurrent",
                stage="story_plan",
                scope_type="story",
                question="choose one",
                options=[
                    DecisionOptionDraft(title="A", summary="A", payload={}),
                    DecisionOptionDraft(title="B", summary="B", payload={}),
                ],
            )
        finally:
            setup.close()

        # Make both workers complete the vulnerable read before either can
        # perform the conditional write. This reproduces the stale-read race
        # that previously let the later writer replace the selected option.
        read_barrier = threading.Barrier(2)

        class BarrierConnection:
            def __init__(self, conn):
                self._conn = conn
                self._paused_once = False

            def execute(self, sql, parameters=()):
                cursor = self._conn.execute(sql, parameters)
                if (
                    not self._paused_once
                    and "SELECT status, selected_option_id FROM decision_sets" in sql
                ):
                    self._paused_once = True
                    read_barrier.wait(timeout=5)
                return cursor

            def commit(self):
                self._conn.commit()

        def select(option_id: str):
            raw = sqlite3.connect(db_path)
            raw.row_factory = sqlite3.Row
            raw.execute("PRAGMA busy_timeout=5000")
            conn = BarrierConnection(raw)
            try:
                resolved = resolve_decision(conn, decision["decision_id"], option_id)
                return "selected", resolved["selected_option_id"]
            except DecisionConflictError:
                return "conflict", option_id
            finally:
                raw.close()

        option_ids = [option["id"] for option in decision["options"]]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(select, option_ids))

        assert sorted(status for status, _ in results) == ["conflict", "selected"]
        selected_id = next(value for status, value in results if status == "selected")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status, selected_option_id FROM decision_sets WHERE id=?",
                (decision["decision_id"],),
            ).fetchone()
            flags = conn.execute(
                "SELECT id, selected FROM decision_options WHERE decision_id=?",
                (decision["decision_id"],),
            ).fetchall()
        finally:
            conn.close()
        assert dict(row) == {"status": "selected", "selected_option_id": selected_id}
        assert [item["id"] for item in flags if item["selected"]] == [selected_id]
