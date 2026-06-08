"""Unit tests for extract_claims_rule."""
import pytest
from novelforge.db.connection import connect, init_db_from_conn
from novelforge.validators.types import ClaimType
from novelforge.validators.claims import extract_claims_rule
from novelforge.ids import new_id


@pytest.fixture
def mem_conn():
    conn = connect(":memory:")
    init_db_from_conn(conn)
    # Seed a power rank
    conn.execute(
        "INSERT INTO power_ranks(id, system_name, rank_name, rank_order) VALUES(?,?,?,?)",
        (new_id("prk"), "练气体系", "金丹·初期", 301),
    )
    conn.commit()
    return conn


class TestExtractClaimsRule:
    def test_extracts_power_rank(self, mem_conn):
        text = "叶凡周身气息暴涨，突破至金丹·初期，俯瞰众人。"
        claims = extract_claims_rule(text, chapter=10, conn=mem_conn)
        power_claims = [c for c in claims if c.ctype == ClaimType.POWER_LEVEL]
        assert len(power_claims) >= 1
        assert any(c.payload.get("rank_label") == "金丹·初期" for c in power_claims)

    def test_extracts_numeric(self, mem_conn):
        text = "叶凡今年18岁，身上还有3块灵石。"
        claims = extract_claims_rule(text, chapter=5, conn=mem_conn)
        numeric_claims = [c for c in claims if c.ctype == ClaimType.NUMERIC]
        assert len(numeric_claims) >= 1

    def test_no_false_positives_on_empty(self, mem_conn):
        text = "天色渐晚，叶凡独自前行。"
        claims = extract_claims_rule(text, chapter=1, conn=mem_conn)
        # Empty text without rank/numeric keywords should not produce many claims
        power_claims = [c for c in claims if c.ctype == ClaimType.POWER_LEVEL]
        assert len(power_claims) == 0
