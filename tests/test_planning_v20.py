"""v20 chapter contract models and append-only planning schema."""
from __future__ import annotations

import json
import sqlite3

import pytest
from pydantic import ValidationError

from novelforge.planning.models import (
    BeatDraft,
    ChapterActuals,
    ChapterPlanDraft,
    ChapterRoutePayload,
    EvidenceSpan,
    PlanAlignmentReport,
)


def _beat(seq: int, beat_type: str, *, hard: bool = False) -> BeatDraft:
    return BeatDraft(
        seq=seq,
        beat_type=beat_type,
        summary=f"beat-{seq}",
        constraint_level="hard" if hard else "soft",
    )


def _chapter(chapter: int) -> ChapterPlanDraft:
    return ChapterPlanDraft(
        chapter=chapter,
        goal=f"推进第{chapter}章目标",
        opening_hook_type="suspense",
        hook_type="promise",
        beats=[_beat(1, "setup", hard=True), _beat(2, "hook")],
    )


class TestChapterRouteContract:
    def test_valid_route_preserves_expanded_contract(self):
        chapter = _chapter(21)
        chapter.arc_targets = ["arc-family"]
        chapter.volume_kr_targets = ["volume-1:kr1"]
        chapter.must_advance = ["找到族谱"]
        chapter.must_not_reveal = ["幕后黑手身份"]
        chapter.target_state = {"evidence_acquired": ["族谱"]}
        chapter.success_criteria = ["正文提供族谱证据"]

        route = ChapterRoutePayload(
            route_title="稳健推进",
            route_summary="连续两章推进家族真相",
            chapters=[chapter, _chapter(22)],
        )

        dumped = route.model_dump()
        assert dumped["chapters"][0]["arc_targets"] == ["arc-family"]
        assert dumped["chapters"][0]["target_state"]["evidence_acquired"] == ["族谱"]

    @pytest.mark.parametrize(
        ("numbers", "message"),
        [
            ([21, 21], "duplicate chapters"),
            ([21, 23], "chapters must be consecutive and ordered"),
            ([22, 21], "chapters must be consecutive and ordered"),
        ],
    )
    def test_route_rejects_duplicate_gap_and_out_of_order(self, numbers, message):
        with pytest.raises(ValidationError, match=message):
            ChapterRoutePayload(
                route_title="非法路线",
                route_summary="bad",
                chapters=[_chapter(number) for number in numbers],
            )

    def test_route_rejects_empty_beats(self):
        chapter = _chapter(1)
        chapter.beats = []
        with pytest.raises(ValidationError, match="must contain beats"):
            ChapterRoutePayload(route_title="x", route_summary="x", chapters=[chapter])

    def test_route_rejects_non_hook_last_beat(self):
        chapter = _chapter(1)
        chapter.beats = [_beat(1, "setup", hard=True), _beat(2, "turn")]
        with pytest.raises(ValidationError, match="last beat must be hook"):
            ChapterRoutePayload(route_title="x", route_summary="x", chapters=[chapter])

    def test_route_rejects_chapter_without_hard_beat(self):
        chapter = _chapter(1)
        chapter.beats = [_beat(1, "setup"), _beat(2, "hook")]
        with pytest.raises(ValidationError, match="must contain a hard beat"):
            ChapterRoutePayload(route_title="x", route_summary="x", chapters=[chapter])

    @pytest.mark.parametrize(
        ("field", "value"),
        [("opening_hook_type", "mystery"), ("hook_type", "new_goal")],
    )
    def test_hook_types_must_use_the_authoritative_enum(self, field, value):
        payload = _chapter(1).model_dump()
        payload[field] = value
        with pytest.raises(ValidationError):
            ChapterPlanDraft.model_validate(payload)

    def test_target_state_requires_success_criteria(self):
        payload = _chapter(1).model_dump()
        payload["target_state"] = {"trust": "broken"}
        payload["success_criteria"] = []
        with pytest.raises(ValidationError, match="success criterion"):
            ChapterPlanDraft.model_validate(payload)


class TestActualsAndAlignmentModels:
    def test_actuals_keep_structured_evidence(self):
        actuals = ChapterActuals.model_validate({
            "chapter": 21,
            "draft_id": "draft-21",
            "events": [{
                "summary": "主角找到族谱",
                "event_type": "discovery",
                "involved_entities": ["protagonist"],
                "evidence_spans": [{
                    "start_char": 12,
                    "end_char": 20,
                    "text": "翻出被涂改的族谱",
                }],
                "confidence": 0.92,
            }],
            "extractor_version": "actuals-v1",
        })
        assert actuals.events[0].evidence_spans[0].start_char == 12
        assert actuals.events[0].confidence == pytest.approx(0.92)

    def test_evidence_offsets_and_scores_are_bounded(self):
        with pytest.raises(ValidationError, match="end_char"):
            EvidenceSpan(start_char=9, end_char=3, text="bad")

        with pytest.raises(ValidationError):
            PlanAlignmentReport(
                chapter=1,
                goal_score=1.1,
                beat_score=1.0,
                arc_score=1.0,
                volume_score=1.0,
                reveal_score=1.0,
                divergence_score=0.0,
                divergence_level="normal",
            )


def _legacy_v19_connection() -> sqlite3.Connection:
    from novelforge.db.connection import connect

    conn = connect(":memory:")
    conn.executescript("""
        CREATE TABLE draft_index (
            id TEXT PRIMARY KEY,
            chapter INTEGER NOT NULL,
            revision_round INTEGER NOT NULL DEFAULT 0,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            UNIQUE(chapter, revision_round)
        );
        CREATE TABLE chapter_cards (
            id TEXT PRIMARY KEY,
            chapter INTEGER NOT NULL UNIQUE,
            title TEXT,
            goal TEXT,
            hook_text TEXT,
            status TEXT NOT NULL DEFAULT 'planned',
            target_emotion TEXT,
            opening_hook_type TEXT,
            hook_type TEXT,
            expectation_score INTEGER,
            plan_version INTEGER NOT NULL DEFAULT 1,
            locked INTEGER NOT NULL DEFAULT 0,
            source_decision_id TEXT
        );
        CREATE TABLE beats (
            id TEXT PRIMARY KEY,
            chapter INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            beat_type TEXT NOT NULL,
            summary TEXT NOT NULL,
            value_axis TEXT,
            arc_id TEXT,
            constraint_level TEXT NOT NULL DEFAULT 'soft',
            UNIQUE(chapter, seq)
        );
    """)
    conn.execute(
        "INSERT INTO chapter_cards("
        " id,chapter,title,goal,hook_text,status,plan_version,locked,source_decision_id)"
        " VALUES('card-21',21,'族谱','寻找族谱','出现新疑点','planned',3,1,'dec-1')"
    )
    conn.executemany(
        "INSERT INTO beats(id,chapter,seq,beat_type,summary,constraint_level)"
        " VALUES(?,?,?,?,?,?)",
        [
            ("beat-1", 21, 1, "setup", "进入藏书阁", "hard"),
            ("beat-2", 21, 2, "hook", "发现涂改痕迹", "hard"),
        ],
    )
    conn.commit()
    return conn


class TestPlanningV20Migration:
    def test_fresh_schema_contains_contract_and_audit_tables(self, tmp_path):
        from novelforge.db.connection import init_db
        from novelforge.db.migrations import column_exists, table_exists

        conn = init_db(tmp_path / "fresh-v20.db")
        for table in (
            "chapter_plan_versions",
            "chapter_plan_version_beats",
            "chapter_actuals",
            "plan_alignment_reports",
        ):
            assert table_exists(conn, table)
        for column in (
            "arc_targets_json",
            "volume_kr_targets_json",
            "milestone_targets_json",
            "must_advance_json",
            "must_resolve_json",
            "must_include_entities_json",
            "must_not_reveal_json",
            "must_not_resolve_json",
            "forbidden_state_changes_json",
            "target_state_json",
            "success_criteria_json",
            "current_plan_version_id",
        ):
            assert column_exists(conn, "chapter_cards", column)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()

    def test_migration_backfills_legacy_card_and_beats_idempotently(self):
        from novelforge.db.migrations.v20 import migrate_v20

        conn = _legacy_v19_connection()
        migrate_v20(conn)
        migrate_v20(conn)
        conn.commit()

        card = conn.execute(
            "SELECT current_plan_version_id, arc_targets_json, target_state_json"
            " FROM chapter_cards WHERE chapter=21"
        ).fetchone()
        assert card["current_plan_version_id"] == "cpv_legacy_21_3"
        assert json.loads(card["arc_targets_json"]) == []
        assert json.loads(card["target_state_json"]) == {}

        version = conn.execute(
            "SELECT * FROM chapter_plan_versions WHERE id='cpv_legacy_21_3'"
        ).fetchone()
        assert version["status"] == "active"
        assert version["source_decision_id"] == "dec-1"
        assert json.loads(version["payload_json"])["goal"] == "寻找族谱"
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM chapter_plan_version_beats"
            " WHERE plan_version_id='cpv_legacy_21_3'"
        ).fetchone()["n"] == 2
        conn.close()

    def test_migration_failure_rolls_back_tables_and_contract_columns(
        self, monkeypatch,
    ):
        import novelforge.db.migrations.v20 as v20
        from novelforge.db.migrations import column_exists, table_exists

        conn = _legacy_v19_connection()
        legacy_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(chapter_cards)")
        }

        def fail_backfill(_conn):
            raise RuntimeError("forced v20 backfill failure")

        monkeypatch.setattr(v20, "_backfill_current_plan_versions", fail_backfill)
        with pytest.raises(RuntimeError, match="forced v20 backfill failure"):
            v20.migrate_v20(conn)

        for table in (
            "chapter_plan_versions",
            "chapter_plan_version_beats",
            "chapter_actuals",
            "plan_alignment_reports",
        ):
            assert not table_exists(conn, table)
        for column in (
            "arc_targets_json",
            "volume_kr_targets_json",
            "milestone_targets_json",
            "must_advance_json",
            "must_resolve_json",
            "must_include_entities_json",
            "must_not_reveal_json",
            "must_not_resolve_json",
            "forbidden_state_changes_json",
            "target_state_json",
            "success_criteria_json",
            "current_plan_version_id",
        ):
            assert not column_exists(conn, "chapter_cards", column)
        assert {
            row["name"] for row in conn.execute("PRAGMA table_info(chapter_cards)")
        } == legacy_columns
        assert conn.execute(
            "SELECT goal FROM chapter_cards WHERE chapter=21"
        ).fetchone()["goal"] == "寻找族谱"
        conn.close()

    def test_only_one_current_actuals_and_alignment_report_per_chapter(self):
        from novelforge.db.migrations.v20 import migrate_v20

        conn = _legacy_v19_connection()
        migrate_v20(conn)
        conn.executemany(
            "INSERT INTO draft_index(id,chapter,revision_round,file_path,sha256)"
            " VALUES(?,?,?,?,?)",
            [
                ("draft-a", 21, 0, "a.md", "sha-a"),
                ("draft-b", 21, 1, "b.md", "sha-b"),
            ],
        )
        conn.execute(
            "INSERT INTO chapter_actuals("
            " id,chapter,draft_id,payload_json,extractor_version)"
            " VALUES('actual-a',21,'draft-a','{}','v1')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO chapter_actuals("
                " id,chapter,draft_id,payload_json,extractor_version)"
                " VALUES('actual-b',21,'draft-b','{}','v1')"
            )
        conn.rollback()

        # The failed statement rolled back the transaction, so recreate the
        # prerequisite rows and exercise the supported supersede-then-insert path.
        migrate_v20(conn)
        conn.executemany(
            "INSERT OR IGNORE INTO draft_index(id,chapter,revision_round,file_path,sha256)"
            " VALUES(?,?,?,?,?)",
            [
                ("draft-a", 21, 0, "a.md", "sha-a"),
                ("draft-b", 21, 1, "b.md", "sha-b"),
            ],
        )
        conn.execute(
            "INSERT INTO chapter_actuals("
            " id,chapter,draft_id,payload_json,extractor_version,status)"
            " VALUES('actual-a',21,'draft-a','{}','v1','superseded')"
        )
        conn.execute(
            "INSERT INTO chapter_actuals("
            " id,chapter,draft_id,payload_json,extractor_version)"
            " VALUES('actual-b',21,'draft-b','{}','v1')"
        )
        conn.execute(
            "INSERT INTO plan_alignment_reports("
            " id,chapter,plan_version_id,actuals_id,goal_score,beat_score,arc_score,"
            " volume_score,reveal_score,divergence_score,divergence_level,detail_json,"
            " evaluator_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "report-a", 21, "cpv_legacy_21_3", "actual-b",
                0.8, 0.8, 0.7, 0.9, 1.0, 0.17, "minor", "{}", "v1",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO plan_alignment_reports("
                " id,chapter,plan_version_id,actuals_id,goal_score,beat_score,arc_score,"
                " volume_score,reveal_score,divergence_score,divergence_level,detail_json,"
                " evaluator_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "report-b", 21, "cpv_legacy_21_3", "actual-b",
                    0.9, 0.9, 0.9, 0.9, 1.0, 0.08, "normal", "{}", "v2",
                ),
            )
        conn.close()
