"""Unit tests for validate_item_inventory."""
import pytest
from novelforge.db.connection import connect, init_db_from_conn
from novelforge.validators.types import Claim, ClaimType, WorldState
from novelforge.validators.items import validate_item_inventory
from novelforge.ids import new_id


@pytest.fixture
def mem_conn():
    conn = connect(":memory:")
    init_db_from_conn(conn)
    return conn


def _seed_entity(conn, name: str, etype: str = "character") -> str:
    eid = new_id("ent")
    conn.execute(
        "INSERT INTO entities(id, canonical_name, entity_type, status) VALUES(?,?,?,?)",
        (eid, name, etype, "active"),
    )
    conn.commit()
    return eid


def _add_item_ownership(conn, item_id: str, owner_id: str, qty: int, chapter: int):
    conn.execute(
        "INSERT INTO item_ownership(id, item_entity_id, owner_entity_id, quantity, since_chapter) "
        "VALUES(?,?,?,?,?)",
        (new_id("io"), item_id, owner_id, qty, chapter),
    )
    conn.commit()


def _make_item_claim(chapter, subject, item_name, op="lose", qty=1) -> Claim:
    return Claim(
        claim_id=new_id("clm"),
        chapter=chapter,
        ctype=ClaimType.ITEM_OWNERSHIP,
        subject_entity=subject,
        span=f"{subject} {op} {item_name}",
        payload={"item": item_name, "op": op, "qty": qty},
    )


class TestItemInventory:
    def test_owned_item_lose_passes(self, mem_conn):
        eid = _seed_entity(mem_conn, "叶凡")
        item_id = _seed_entity(mem_conn, "飞升丹", "item")
        _add_item_ownership(mem_conn, item_id, eid, qty=3, chapter=1)
        world = WorldState(as_of=9, conn=mem_conn)
        claim = _make_item_claim(10, "叶凡", "飞升丹", op="consume", qty=1)
        issues = validate_item_inventory([claim], world, mem_conn)
        assert issues == [], f"Should pass: {[i.code for i in issues]}"

    def test_unowned_item_fails(self, mem_conn):
        _seed_entity(mem_conn, "叶凡")
        _seed_entity(mem_conn, "飞升丹", "item")
        # No ownership record
        world = WorldState(as_of=9, conn=mem_conn)
        claim = _make_item_claim(10, "叶凡", "飞升丹", op="consume", qty=1)
        issues = validate_item_inventory([claim], world, mem_conn)
        codes = [i.code for i in issues]
        assert "ITEM_NOT_OWNED" in codes or "ITEM_DOUBLE_SPEND" in codes

    def test_insufficient_qty_fails(self, mem_conn):
        eid = _seed_entity(mem_conn, "叶凡")
        item_id = _seed_entity(mem_conn, "飞升丹", "item")
        _add_item_ownership(mem_conn, item_id, eid, qty=1, chapter=1)
        world = WorldState(as_of=9, conn=mem_conn)
        claim = _make_item_claim(10, "叶凡", "飞升丹", op="consume", qty=5)
        issues = validate_item_inventory([claim], world, mem_conn)
        assert any(i.code in {"ITEM_NOT_OWNED", "ITEM_DOUBLE_SPEND"} for i in issues)
