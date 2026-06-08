"""Unit tests for validate_knowledge_edges.
3 cases: known info OK, unknown info leaks, public info OK.
"""
import sqlite3
import pytest
from novelforge.db.connection import connect, init_db_from_conn
from novelforge.validators.types import Claim, ClaimType, WorldState
from novelforge.validators.knowledge import validate_knowledge_edges
from novelforge.ids import new_id


@pytest.fixture
def mem_conn():
    conn = connect(":memory:")
    init_db_from_conn(conn)
    return conn


def _seed_entity(conn, name: str = "叶凡") -> str:
    eid = new_id("ent")
    conn.execute(
        "INSERT INTO entities(id, canonical_name, entity_type, status) VALUES(?,?,?,?)",
        (eid, name, "character", "active"),
    )
    conn.commit()
    return eid


def _add_knowledge(conn, knower_id: str, secret_key: str, chapter: int,
                   public_from_chapter=None, state="knows"):
    conn.execute(
        "INSERT INTO knowledge_edges(id, knower_entity_id, secret_key, knowledge_state, "
        "learned_chapter, public_from_chapter) VALUES(?,?,?,?,?,?)",
        (new_id("ke"), knower_id, secret_key, state, chapter, public_from_chapter),
    )
    conn.commit()


def _make_knowledge_claim(chapter: int, subject: str, info_key: str, act: str = "reference") -> Claim:
    return Claim(
        claim_id=new_id("clm"),
        chapter=chapter,
        ctype=ClaimType.KNOWLEDGE,
        subject_entity=subject,
        span=f"提到了{info_key}",
        payload={"info_key": info_key, "act": act},
    )


class TestKnownInfo:
    """Entity knowing the info → no issue."""

    def test_known_info_passes(self, mem_conn):
        eid = _seed_entity(mem_conn)
        _add_knowledge(mem_conn, eid, "villain_identity", chapter=3)
        world = WorldState(as_of=9, conn=mem_conn)
        # subject_entity must be the entity ID so knowledge_set() matches knower_entity_id
        claim = _make_knowledge_claim(chapter=10, subject=eid, info_key="villain_identity")
        issues = validate_knowledge_edges([claim], world, mem_conn)
        assert issues == [], f"Should pass for known info: {[i.code for i in issues]}"


class TestUnknownInfoLeak:
    """Entity referencing unknown info → KNOWLEDGE_LEAK."""

    def test_unknown_info_leaks(self, mem_conn):
        eid = _seed_entity(mem_conn, "叶凡")
        # No knowledge_edges added for villain_identity
        world = WorldState(as_of=9, conn=mem_conn)
        claim = _make_knowledge_claim(chapter=10, subject=eid, info_key="villain_identity")
        issues = validate_knowledge_edges([claim], world, mem_conn)
        codes = [i.code for i in issues]
        assert "KNOWLEDGE_LEAK" in codes, f"Expected KNOWLEDGE_LEAK: {codes}"

    def test_exempt_tag_suppresses_leak(self, mem_conn):
        eid = _seed_entity(mem_conn, "叶凡")
        world = WorldState(as_of=9, conn=mem_conn)
        claim = _make_knowledge_claim(chapter=10, subject=eid, info_key="villain_identity")
        claim.exempt_tags = ["planted_misdirection"]
        issues = validate_knowledge_edges([claim], world, mem_conn)
        assert issues == [], "Exempt tag should suppress KNOWLEDGE_LEAK"


class TestPublicInfo:
    """Public info (public_from_chapter set) → no issue even without knowledge edge."""

    def test_public_info_passes(self, mem_conn):
        eid = _seed_entity(mem_conn, "叶凡")
        # Info becomes public at chapter 5, entity "someone" learned it
        other_id = _seed_entity(mem_conn, "某人")
        _add_knowledge(mem_conn, other_id, "villain_identity", chapter=5, public_from_chapter=5)
        world = WorldState(as_of=9, conn=mem_conn)
        # 叶凡 references villain_identity at chapter 10 (after it's public)
        # subject_entity is eid (no personal knowledge edge), but info is public → no leak
        claim = _make_knowledge_claim(chapter=10, subject=eid, info_key="villain_identity")
        issues = validate_knowledge_edges([claim], world, mem_conn)
        assert issues == [], f"Public info should not leak: {[i.code for i in issues]}"
