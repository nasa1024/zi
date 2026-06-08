"""Unit tests for validate_power_monotonicity.
3 cases: legal rise, illegal regression, illegal skip.
"""
import sqlite3
import pathlib
import pytest
from novelforge.db.connection import connect, init_db_from_conn
from novelforge.validators.types import Claim, ClaimType, WorldState
from novelforge.validators.power import validate_power_monotonicity
from novelforge.ids import new_id


@pytest.fixture
def mem_conn():
    """Fresh :memory: SQLite with full schema."""
    conn = connect(":memory:")
    init_db_from_conn(conn)
    return conn


def _seed_power(conn, entity_name: str = "叶凡", system: str = "练气体系"):
    """Seed an entity and a 3-level power system."""
    ent_id = new_id("ent")
    conn.execute(
        "INSERT INTO entities(id, canonical_name, entity_type, status) VALUES(?,?,?,?)",
        (ent_id, entity_name, "character", "active"),
    )
    ranks = [
        (new_id("prk"), system, "炼气·初期", 101),
        (new_id("prk"), system, "筑基·初期", 201),
        (new_id("prk"), system, "金丹·初期", 301),
    ]
    for rid, sn, rn, ro in ranks:
        conn.execute(
            "INSERT INTO power_ranks(id, system_name, rank_name, rank_order) VALUES(?,?,?,?)",
            (rid, sn, rn, ro),
        )
    conn.commit()
    return ent_id, ranks


def _add_power_log(conn, entity_id, system, rank_name, rank_order, chapter, change_type="breakthrough"):
    rid = new_id("cpl")
    rank_id = conn.execute("SELECT id FROM power_ranks WHERE rank_name=?", (rank_name,)).fetchone()[0]
    conn.execute(
        "INSERT INTO character_power_log(id, entity_id, system_name, rank_id, rank_order, change_chapter, change_type) "
        "VALUES(?,?,?,?,?,?,?)",
        (rid, entity_id, system, rank_id, rank_order, chapter, change_type),
    )
    conn.commit()


def _make_power_claim(chapter: int, rank_label: str, subject_entity_id: str, direction: str = "up") -> Claim:
    """Create a POWER_LEVEL Claim using the entity_id (not human name) as subject_entity.

    validate_power_monotonicity passes subject_entity directly to
    WorldState.power_history() which queries character_power_log.entity_id,
    so we must supply the database entity_id here.
    """
    return Claim(
        claim_id=new_id("clm"),
        chapter=chapter,
        ctype=ClaimType.POWER_LEVEL,
        subject_entity=subject_entity_id,
        span=f"突破至{rank_label}",
        payload={"rank_label": rank_label, "direction": direction},
    )


class TestLegalRise:
    """Legal power advancement should produce no issues."""

    def test_legal_single_step_rise(self, mem_conn):
        ent_id, ranks = _seed_power(mem_conn)
        _add_power_log(mem_conn, ent_id, "练气体系", "炼气·初期", 101, chapter=1)

        world = WorldState(as_of=9, conn=mem_conn)
        claim = _make_power_claim(chapter=10, rank_label="筑基·初期", subject_entity_id=ent_id, direction="up")
        issues = validate_power_monotonicity([claim], world, mem_conn)
        # 1 big-boundary jump (炼气→筑基) == MAX_JUMP_THRESHOLD → no issue
        assert issues == [], f"Expected no issues but got: {[i.code for i in issues]}"


class TestIllegalRegression:
    """Power regression without legal annotation should produce POWER_REGRESSION."""

    def test_unexplained_regression(self, mem_conn):
        ent_id, ranks = _seed_power(mem_conn)
        # History: chapter 5 = 筑基
        _add_power_log(mem_conn, ent_id, "练气体系", "筑基·初期", 201, chapter=5)

        world = WorldState(as_of=9, conn=mem_conn)
        # Claim: chapter 10 = 炼气 (lower rank_order) without reason_tag
        claim = _make_power_claim(chapter=10, rank_label="炼气·初期", subject_entity_id=ent_id, direction="down")
        issues = validate_power_monotonicity([claim], world, mem_conn)
        codes = [i.code for i in issues]
        assert "POWER_REGRESSION" in codes, f"Expected POWER_REGRESSION but got: {codes}"

    def test_legal_injury_drop(self, mem_conn):
        ent_id, ranks = _seed_power(mem_conn)
        _add_power_log(mem_conn, ent_id, "练气体系", "筑基·初期", 201, chapter=5)

        world = WorldState(as_of=9, conn=mem_conn)
        claim = _make_power_claim(chapter=10, rank_label="炼气·初期", subject_entity_id=ent_id, direction="down")
        claim.payload["reason_tag"] = "injury_drop"  # Legal drop
        issues = validate_power_monotonicity([claim], world, mem_conn)
        codes = [i.code for i in issues]
        assert "POWER_REGRESSION" not in codes, f"Should not report POWER_REGRESSION for legal drop: {codes}"


class TestIllegalSkip:
    """Power skip across >1 big boundary without aid should produce POWER_LEVEL_SKIP."""

    def test_two_boundary_skip_no_aid(self, mem_conn):
        ent_id, ranks = _seed_power(mem_conn)
        _add_power_log(mem_conn, ent_id, "练气体系", "炼气·初期", 101, chapter=1)

        world = WorldState(as_of=9, conn=mem_conn)
        # Claim: jump from 炼气(101) to 金丹(301) = 2 big boundaries → skip
        claim = _make_power_claim(chapter=10, rank_label="金丹·初期", subject_entity_id=ent_id, direction="up")
        issues = validate_power_monotonicity([claim], world, mem_conn)
        codes = [i.code for i in issues]
        assert "POWER_LEVEL_SKIP" in codes, f"Expected POWER_LEVEL_SKIP but got: {codes}"
