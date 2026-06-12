"""P2#12 原型：validate_event_visibility（角色知道了不在场的事）+ refine_knowledge_claims。

设计：docs/superpowers/specs/2026-06-13-p2-visibility-validator-design.md
"""
import json

import pytest

from novelforge.db.connection import connect, init_db_from_conn
from novelforge.ids import new_id
from novelforge.validators.types import Claim, ClaimType, WorldState


@pytest.fixture
def mem_conn():
    conn = connect(":memory:")
    init_db_from_conn(conn)
    return conn


def _seed_entity(conn, name="叶凡") -> str:
    eid = new_id("ent")
    conn.execute(
        "INSERT INTO entities(id, canonical_name, entity_type, status) VALUES(?,?,?,?)",
        (eid, name, "character", "active"))
    conn.commit()
    return eid


def _seed_event(conn, title, chapter, participants=None):
    conn.execute(
        "INSERT INTO timeline_events(id, title, chapter, story_time_start,"
        " story_time_end) VALUES(?,?,?,0,1)",
        (new_id("tl"), title, chapter))
    if participants is not None:
        conn.execute(
            "UPDATE timeline_events SET participants=? WHERE title=?",
            (json.dumps(participants, ensure_ascii=False), title))
    conn.commit()


def _add_edge(conn, knower_id, secret_key, chapter, public_from=None):
    conn.execute(
        "INSERT INTO knowledge_edges(id, knower_entity_id, secret_key,"
        " knowledge_state, learned_chapter, public_from_chapter)"
        " VALUES(?,?,?,'knows',?,?)",
        (new_id("ke"), knower_id, secret_key, chapter, public_from))
    conn.commit()


def _claim(chapter, subject, info_key):
    return Claim(
        claim_id=new_id("clm"), chapter=chapter, ctype=ClaimType.KNOWLEDGE,
        subject_entity=subject, span=f"{subject}知道了{info_key}",
        payload={"info_key": info_key, "act": "reference"})


class TestEventVisibility:
    def _run(self, conn, claims, as_of=9):
        from novelforge.validators.presence import validate_event_visibility
        world = WorldState(as_of=as_of, conn=conn)
        return validate_event_visibility(claims, world, conn)

    def test_absent_from_event_flags(self, mem_conn):
        """事发时不在场、无知情边、不公开 → KNOWLEDGE_NO_PRESENCE。"""
        _seed_entity(mem_conn, "叶凡")
        other = _seed_entity(mem_conn, "萧炎")
        _seed_event(mem_conn, "血衣门密谋", 3, participants=[other])
        issues = self._run(mem_conn, [_claim(10, "叶凡", "血衣门密谋")])
        assert len(issues) == 1
        assert issues[0].code == "KNOWLEDGE_NO_PRESENCE"
        assert issues[0].severity == "major"
        assert "血衣门密谋" in issues[0].message

    def test_present_at_event_silent(self, mem_conn):
        eid = _seed_entity(mem_conn, "叶凡")
        _seed_event(mem_conn, "血衣门密谋", 3, participants=[eid])
        assert self._run(mem_conn, [_claim(10, "叶凡", "血衣门密谋")]) == []

    def test_present_by_name_silent(self, mem_conn):
        """participants 存的是名字而非 id 也认。"""
        _seed_entity(mem_conn, "叶凡")
        _seed_event(mem_conn, "血衣门密谋", 3, participants=["叶凡"])
        assert self._run(mem_conn, [_claim(10, "叶凡", "血衣门密谋")]) == []

    def test_has_knowledge_edge_silent(self, mem_conn):
        """有人转告过（有知情边）→ 不在场也合法。"""
        eid = _seed_entity(mem_conn, "叶凡")
        other = _seed_entity(mem_conn, "萧炎")
        _seed_event(mem_conn, "血衣门密谋", 3, participants=[other])
        _add_edge(mem_conn, eid, "血衣门密谋", 5)
        assert self._run(mem_conn, [_claim(10, "叶凡", "血衣门密谋")]) == []

    def test_public_info_silent(self, mem_conn):
        _seed_entity(mem_conn, "叶凡")
        other = _seed_entity(mem_conn, "萧炎")
        _seed_event(mem_conn, "血衣门密谋", 3, participants=[other])
        _add_edge(mem_conn, other, "血衣门密谋", 3, public_from=4)
        assert self._run(mem_conn, [_claim(10, "叶凡", "血衣门密谋")]) == []

    def test_no_matching_event_silent(self, mem_conn):
        """找不到对应事件 → 没有在场证据可查，沉默（LEAK 兜底）。"""
        _seed_entity(mem_conn, "叶凡")
        assert self._run(mem_conn, [_claim(10, "叶凡", "惊天秘密")]) == []

    def test_unresolvable_subject_silent(self, mem_conn):
        """主语解析不到实体 → 抽取噪声，沉默。"""
        _seed_event(mem_conn, "血衣门密谋", 3, participants=[])
        assert self._run(mem_conn, [_claim(10, "路人甲", "血衣门密谋")]) == []

    def test_fuzzy_event_title_match(self, mem_conn):
        """info_key 与事件标题 bigram 重叠 ≥0.6 也算同一事件。"""
        _seed_entity(mem_conn, "叶凡")
        other = _seed_entity(mem_conn, "萧炎")
        _seed_event(mem_conn, "血衣门的密谋", 3, participants=[other])
        issues = self._run(mem_conn, [_claim(10, "叶凡", "血衣门密谋")])
        assert len(issues) == 1

    def test_exempt_tag_silent(self, mem_conn):
        _seed_entity(mem_conn, "叶凡")
        other = _seed_entity(mem_conn, "萧炎")
        _seed_event(mem_conn, "血衣门密谋", 3, participants=[other])
        c = _claim(10, "叶凡", "血衣门密谋")
        c.exempt_tags = ["unreliable_narrator"]
        assert self._run(mem_conn, [c]) == []


class TestRefineKnowledgeClaims:
    """接线层精炼：正则抽出的自由文本 → 实体 id + 账本 secret_key；匹配不上的丢弃。"""

    def test_resolves_name_and_key(self, mem_conn):
        from novelforge.validators.presence import refine_knowledge_claims
        eid = _seed_entity(mem_conn, "叶凡")
        other = _seed_entity(mem_conn, "萧炎")
        _add_edge(mem_conn, other, "血衣门密谋", 3)
        refined = refine_knowledge_claims([_claim(10, "叶凡", "血衣门密谋")], mem_conn)
        assert len(refined) == 1
        assert refined[0].subject_entity == eid
        assert refined[0].payload["info_key"] == "血衣门密谋"

    def test_unknown_key_dropped(self, mem_conn):
        """info_key 不在账本里 → 无法确定性判定，不送 LEAK 检查。"""
        from novelforge.validators.presence import refine_knowledge_claims
        _seed_entity(mem_conn, "叶凡")
        assert refine_knowledge_claims([_claim(10, "叶凡", "惊天秘密")], mem_conn) == []

    def test_unknown_subject_dropped(self, mem_conn):
        from novelforge.validators.presence import refine_knowledge_claims
        other = _seed_entity(mem_conn, "萧炎")
        _add_edge(mem_conn, other, "血衣门密谋", 3)
        assert refine_knowledge_claims([_claim(10, "路人甲", "血衣门密谋")], mem_conn) == []

    def test_fuzzy_key_canonicalized(self, mem_conn):
        """近似 key（"血衣门的密谋"）归一到账本 key（"血衣门密谋"）。"""
        from novelforge.validators.presence import refine_knowledge_claims
        _seed_entity(mem_conn, "叶凡")
        other = _seed_entity(mem_conn, "萧炎")
        _add_edge(mem_conn, other, "血衣门密谋", 3)
        refined = refine_knowledge_claims([_claim(10, "叶凡", "血衣门的密谋")], mem_conn)
        assert len(refined) == 1
        assert refined[0].payload["info_key"] == "血衣门密谋"

    def test_non_knowledge_claims_untouched(self, mem_conn):
        from novelforge.validators.presence import refine_knowledge_claims
        c = Claim(claim_id=new_id("clm"), chapter=1, ctype=ClaimType.POWER_LEVEL,
                  span="筑基", payload={"rank_label": "筑基"})
        assert refine_knowledge_claims([c], mem_conn) == []
