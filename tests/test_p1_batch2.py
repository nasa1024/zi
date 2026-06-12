"""P1 第二批测试：细纲契约（#7）/ 文风锚点（#9）/ 爽点循环完成率（#10）。

spec: docs/superpowers/specs/2026-06-13-p1-batch2-design.md
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def conn():
    from novelforge.db.connection import init_db_from_conn
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db_from_conn(c)
    yield c
    c.close()


# ── API fixtures（与 test_p1_core 同款）──────────────────────────────────────

@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_DATA", str(tmp_path))
    import novelforge.app.deps as deps_mod
    deps_mod._registry = None
    yield tmp_path
    deps_mod._registry = None


@pytest.fixture
def client(tmp_data):
    from novelforge.app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def project(client):
    resp = client.post("/v1/projects", json={"name": "P1批2测试", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


# ── #7 钩子枚举与归一 ─────────────────────────────────────────────────────────

class TestHookNormalize:
    def test_exact_enum_key_passthrough(self):
        from novelforge.craft.hooks import normalize_hook_type
        assert normalize_hook_type("reversal", "ending") == "reversal"
        assert normalize_hook_type("suspense", "opening") == "suspense"

    def test_chinese_keyword_maps_to_enum(self):
        from novelforge.craft.hooks import normalize_hook_type
        assert normalize_hook_type("反转式", "ending") == "reversal"
        assert normalize_hook_type("命悬一线", "ending") == "cliffhanger"
        assert normalize_hook_type("悬念开局", "opening") == "suspense"

    def test_unrecognizable_returns_other(self):
        from novelforge.craft.hooks import normalize_hook_type
        assert normalize_hook_type("写得很好看", "ending") == "other"
        assert normalize_hook_type("", "ending") == "other"
        assert normalize_hook_type(None, "opening") == "other"

    def test_wrong_kind_enum_not_passthrough(self):
        from novelforge.craft.hooks import normalize_hook_type
        # cliffhanger 是章尾式，不在章首 7 式里
        assert normalize_hook_type("cliffhanger", "opening") == "other"

    def test_hook_label_renders_chinese(self):
        from novelforge.craft.hooks import hook_label
        assert hook_label("reversal") == "反转"
        assert hook_label("other") == "other"


# ── #7 volume_plan 生产侧 ─────────────────────────────────────────────────────

class TestVolumePlanContract:
    def test_system_prompt_lists_hook_enums(self):
        from novelforge.skills.volume_plan_skill import _SYSTEM
        for marker in ("target_emotion", "opening_hook_type", "hook_type",
                       "expectation_score", "悬念", "反转", "相邻两章"):
            assert marker in _SYSTEM, f"prompt 缺契约标记: {marker}"

    def test_parse_normalizes_and_clamps(self):
        from novelforge.skills.volume_plan_skill import _parse_plans
        raw = json.dumps([{
            "chapter": 3, "title": "t", "goal": "g", "hook_text": "h",
            "target_emotion": "紧张",
            "opening_hook_type": "危机开局",
            "hook_type": "大反转",
            "expectation_score": 9,
            "beats": [{"beat_type": "hook", "summary": "s"}],
        }], ensure_ascii=False)
        plans = _parse_plans(f"```plans\n{raw}\n```", 3, 3)
        assert plans[0]["opening_hook_type"] == "crisis"
        assert plans[0]["hook_type"] == "reversal"
        assert plans[0]["expectation_score"] == 5      # clamp 到 1-5
        assert plans[0]["target_emotion"] == "紧张"

    def test_parse_missing_fields_tolerant(self):
        from novelforge.skills.volume_plan_skill import _parse_plans
        raw = json.dumps([{"chapter": 3, "title": "t", "goal": "g",
                           "hook_text": "h", "beats": []}], ensure_ascii=False)
        plans = _parse_plans(f"```plans\n{raw}\n```", 3, 3)
        assert plans[0]["hook_type"] == "other"
        assert plans[0]["expectation_score"] is None


# ── #7 消费侧：chapter_goal 注入 + 评委 ground truth ──────────────────────────

class TestContractConsumption:
    def test_chapter_goal_includes_contract_line(self, conn):
        from novelforge.app.chapter_suggest import assemble_chapter_goal
        conn.execute(
            "INSERT INTO chapter_cards(id, chapter, title, goal, target_emotion,"
            " opening_hook_type, hook_type, expectation_score)"
            " VALUES('c1', 5, '风起', '主角入城', '紧张', 'crisis', 'reversal', 4)")
        conn.commit()
        goal, sources = assemble_chapter_goal(conn, 5)
        assert "细纲契约" in goal
        assert "紧张" in goal and "危机" in goal and "反转" in goal and "4/5" in goal
        assert "chapter_card_contract" in sources

    def test_chapter_goal_no_contract_when_fields_empty(self, conn):
        from novelforge.app.chapter_suggest import assemble_chapter_goal
        conn.execute(
            "INSERT INTO chapter_cards(id, chapter, title, goal) VALUES('c1', 5, 't', 'g')")
        conn.commit()
        goal, sources = assemble_chapter_goal(conn, 5)
        assert "细纲契约" not in goal
        assert "chapter_card_contract" not in sources

    def test_judge_prompts_mention_contract(self):
        from novelforge.craft.candidate_judge import _JUDGE_SYSTEM, _SCORE_SYSTEM
        assert "承诺" in _JUDGE_SYSTEM
        assert "承诺" in _SCORE_SYSTEM


# ── #7 确定性检查：连续两章同型钩子 ───────────────────────────────────────────

def _seed_cards(conn, *rows):
    """rows: (chapter, hook_type)"""
    for i, (ch, ht) in enumerate(rows):
        conn.execute(
            "INSERT INTO chapter_cards(id, chapter, hook_type) VALUES(?,?,?)",
            (f"card{i}", ch, ht))
    conn.commit()


class TestHookRepeatCheck:
    def _run(self, conn, chapter):
        from novelforge.skills.craft_check_skill import _check_hook_repeat
        return _check_hook_repeat(conn, chapter)

    def test_same_type_adjacent_warns(self, conn):
        _seed_cards(conn, (4, "reversal"), (5, "reversal"))
        issues = self._run(conn, 5)
        assert len(issues) == 1
        assert issues[0].severity == "warn"
        assert issues[0].check == "hook_repeat"

    def test_different_type_no_warn(self, conn):
        _seed_cards(conn, (4, "reversal"), (5, "cliffhanger"))
        assert self._run(conn, 5) == []

    def test_other_or_missing_skipped(self, conn):
        _seed_cards(conn, (4, "other"), (5, "other"))
        assert self._run(conn, 5) == []
        assert self._run(conn, 99) == []   # 无卡

    def test_null_hook_type_skipped(self, conn):
        _seed_cards(conn, (4, None), (5, "reversal"))
        assert self._run(conn, 5) == []


# ── #9 style_anchors API ─────────────────────────────────────────────────────

class TestStyleAnchorApi:
    def test_crud_roundtrip(self, client, project):
        r = client.post(f"/v1/{project}/style-anchors", json={
            "emotion": "紧张", "title": "某书第3章", "content": "刀光一闪。" * 20})
        assert r.status_code == 201
        aid = r.json()["id"]
        rows = client.get(f"/v1/{project}/style-anchors",
                          params={"emotion": "紧张"}).json()
        assert len(rows) == 1 and rows[0]["enabled"] is True
        r = client.patch(f"/v1/{project}/style-anchors/{aid}", json={"enabled": False})
        assert r.json()["enabled"] is False
        assert client.delete(f"/v1/{project}/style-anchors/{aid}").status_code == 204
        assert client.get(f"/v1/{project}/style-anchors").json() == []

    def test_content_length_validated(self, client, project):
        r = client.post(f"/v1/{project}/style-anchors",
                        json={"emotion": "x", "content": "短"})
        assert r.status_code == 422

    def test_patch_missing_404(self, client, project):
        r = client.patch(f"/v1/{project}/style-anchors/nope", json={"enabled": False})
        assert r.status_code == 404
