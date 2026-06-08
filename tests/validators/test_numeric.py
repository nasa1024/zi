"""Unit tests for validate_numeric_conservation."""
import pytest
from novelforge.db.connection import connect, init_db_from_conn
from novelforge.validators.types import Claim, ClaimType, WorldState
from novelforge.validators.numeric import validate_numeric_conservation
from novelforge.ids import new_id


@pytest.fixture
def mem_conn():
    conn = connect(":memory:")
    init_db_from_conn(conn)
    return conn


def _seed_entity(conn, name: str) -> str:
    eid = new_id("ent")
    conn.execute(
        "INSERT INTO entities(id, canonical_name, entity_type, status) VALUES(?,?,?,?)",
        (eid, name, "character", "active"),
    )
    conn.commit()
    return eid


def _add_numeric(conn, entity_id, key, value, unit, chapter):
    conn.execute(
        "INSERT INTO numeric_facts(id, entity_id, metric_key, value, unit, as_of_chapter, monotonic) "
        "VALUES(?,?,?,?,?,?,?)",
        (new_id("nf"), entity_id, key, value, unit, chapter, "none"),
    )
    conn.commit()


def _make_numeric_claim(chapter, subject, key, value, unit, op="set") -> Claim:
    return Claim(
        claim_id=new_id("clm"),
        chapter=chapter,
        ctype=ClaimType.NUMERIC,
        subject_entity=subject,
        span=f"{value}{unit}",
        payload={"key": key, "value": value, "unit": unit, "op": op},
    )


class TestNumericConservation:
    def test_set_consistent_passes(self, mem_conn):
        eid = _seed_entity(mem_conn, "叶凡")
        _add_numeric(mem_conn, eid, "age", 18.0, "岁", chapter=1)
        world = WorldState(as_of=9, conn=mem_conn)
        # subject_entity must be the entity ID so numeric_state() matches entity_id
        claim = _make_numeric_claim(10, eid, "age", 18.0, "岁", "set")
        issues = validate_numeric_conservation([claim], world, mem_conn)
        assert issues == [], f"Consistent set should pass: {[i.code for i in issues]}"

    def test_set_contradiction_fails(self, mem_conn):
        eid = _seed_entity(mem_conn, "叶凡")
        _add_numeric(mem_conn, eid, "age", 18.0, "岁", chapter=1)
        world = WorldState(as_of=9, conn=mem_conn)
        claim = _make_numeric_claim(10, eid, "age", 16.0, "岁", "set")
        issues = validate_numeric_conservation([claim], world, mem_conn)
        codes = [i.code for i in issues]
        assert "NUMERIC_CONTRADICTION" in codes

    def test_sub_negative_balance_fails(self, mem_conn):
        eid = _seed_entity(mem_conn, "叶凡")
        _add_numeric(mem_conn, eid, "spirit_stones", 5.0, "块", chapter=1)
        world = WorldState(as_of=9, conn=mem_conn)
        claim = _make_numeric_claim(10, eid, "spirit_stones", 10.0, "块", "sub")
        issues = validate_numeric_conservation([claim], world, mem_conn)
        codes = [i.code for i in issues]
        assert "NUMERIC_NEGATIVE_BALANCE" in codes
