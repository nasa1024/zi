"""M4 测试：④ volume_plan 卷级批量预规划。全部 FakeProvider，无网络。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from novelforge.skills.volume_plan_skill import _parse_plans


# ── fixtures ──────────────────────────────────────────────────────────────────

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
    resp = client.post("/v1/projects", json={"name": "M4测试", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


def _open_conn(project_id):
    from novelforge.app.deps import get_registry
    return get_registry().open_conn(project_id)


def _plans_response(chapters: list[int]) -> str:
    items = []
    for ch in chapters:
        items.append({
            "chapter": ch,
            "title": f"第{ch}章标题",
            "goal": f"第{ch}章目标：主角遭遇冲突并爆发爽点",
            "hook_text": f"第{ch}章末黑影逼近",
            "beats": [
                {"beat_type": "setup", "summary": "铺垫", "value_axis": "平静→紧张"},
                {"beat_type": "turn", "summary": "转折", "value_axis": "紧张→危机"},
                {"beat_type": "hook", "summary": "钩子", "value_axis": "危机→悬念"},
            ],
        })
    return "```plans\n" + json.dumps(items, ensure_ascii=False) + "\n```"


def _patch_gateway(monkeypatch, response_text: str):
    """让 volumes API 的 build_gateway 返回 FakeProvider 网关。"""
    import novelforge.control_plane.llm.factory as factory_mod
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway

    def fake_build(cfg, ledger=None):
        return LLMGateway(
            FakeProvider(responses=[response_text]),
            BudgetLedger(max_tokens=1_000_000, max_usd=10.0),
        )

    monkeypatch.setattr(factory_mod, "build_gateway", fake_build)


def _create_volume(client, project, *, start=1, end=10):
    r = client.post(f"/v1/{project}/volumes", json={
        "volume_no": 1, "title": "初入宗门", "synopsis": "主角从凡人到外门弟子",
        "start_chapter": start, "end_chapter": end,
    })
    assert r.status_code == 201


# ── _parse_plans 单元测试 ─────────────────────────────────────────────────────

class TestParsePlans:
    def test_normal_block(self):
        plans = _parse_plans(_plans_response([1, 2, 3]), 1, 3)
        assert [p["chapter"] for p in plans] == [1, 2, 3]
        assert plans[0]["hook_text"] == "第1章末黑影逼近"
        assert plans[0]["beats"][0]["seq"] == 1

    def test_out_of_range_filtered(self):
        plans = _parse_plans(_plans_response([1, 2, 99]), 1, 3)
        assert [p["chapter"] for p in plans] == [1, 2]

    def test_truncated_json_rescued(self):
        full = json.dumps([{"chapter": 1, "title": "t", "goal": "g",
                            "hook_text": "h", "beats": []}], ensure_ascii=False)
        truncated = "```plans\n" + full[:-1]   # 去掉收尾 ]
        plans = _parse_plans(truncated, 1, 3)
        assert len(plans) == 1

    def test_invalid_beat_type_dropped(self):
        raw = ("```plans\n"
               '[{"chapter":1,"title":"t","goal":"g","hook_text":"h",'
               '"beats":[{"beat_type":"bogus","summary":"x"},'
               '{"beat_type":"hook","summary":"y"}]}]\n```')
        plans = _parse_plans(raw, 1, 1)
        assert len(plans[0]["beats"]) == 1
        assert plans[0]["beats"][0]["beat_type"] == "hook"

    def test_garbage_returns_empty(self):
        assert _parse_plans("不是 JSON", 1, 3) == []


# ── API 集成测试 ──────────────────────────────────────────────────────────────

class TestVolumePlanAPI:
    def test_plan_persists_cards_and_feeds_next_suggestion(self, client, project, monkeypatch):
        _create_volume(client, project)
        _patch_gateway(monkeypatch, _plans_response([1, 2, 3]))

        r = client.post(f"/v1/{project}/volumes/1/plan", json={"to_chapter": 3})
        assert r.status_code == 200
        body = r.json()
        assert body["error"] is None
        assert [c["chapter"] for c in body["planned"]] == [1, 2, 3]
        assert body["planned"][0]["beats"][-1]["beat_type"] == "hook"

        # 章节卡入库 → /pipeline/next 立即变为大纲驱动
        nxt = client.get(f"/v1/{project}/pipeline/next").json()
        assert nxt["next_chapter"] == 1
        assert "chapter_card" in nxt["sources"]
        assert "第1章目标" in nxt["suggested_goal"]
        assert "beats" in nxt["sources"]

    def test_drafted_chapter_protected(self, client, project, monkeypatch):
        """已有草稿/已非 planned 的章不被覆盖。"""
        _create_volume(client, project)
        conn = _open_conn(project)
        conn.execute(
            "INSERT INTO draft_index(id, chapter, revision_round, file_path, sha256, word_count)"
            " VALUES('d1', 2, 0, 'l0/ch0002_r00.txt', 'x', 3000)",
        )
        conn.execute(
            "INSERT INTO chapter_cards(id, chapter, title, goal, status)"
            " VALUES('cc2', 2, '已写章', '已写目标', 'drafted')",
        )
        conn.commit()
        conn.close()

        _patch_gateway(monkeypatch, _plans_response([1, 2, 3]))
        r = client.post(f"/v1/{project}/volumes/1/plan", json={"from_chapter": 1, "to_chapter": 3})
        body = r.json()
        assert body["skipped"] == [2]
        assert [c["chapter"] for c in body["planned"]] == [1, 3]

        conn = _open_conn(project)
        row = conn.execute("SELECT goal FROM chapter_cards WHERE chapter=2").fetchone()
        conn.close()
        assert row["goal"] == "已写目标"   # 未被覆盖

    def test_range_clamped_to_10_chapters(self, client, project, monkeypatch):
        _create_volume(client, project, start=1, end=50)
        _patch_gateway(monkeypatch, _plans_response(list(range(1, 11))))
        r = client.post(f"/v1/{project}/volumes/1/plan", json={})
        body = r.json()
        assert body["from_chapter"] == 1
        assert body["to_chapter"] == 10   # ≤10 章

    def test_replan_overwrites_planned(self, client, project, monkeypatch):
        _create_volume(client, project)
        _patch_gateway(monkeypatch, _plans_response([1]))
        assert client.post(f"/v1/{project}/volumes/1/plan",
                           json={"to_chapter": 1}).json()["planned"]

        # 二次规划：同章 planned 卡可被覆盖，beats 不重复堆积
        _patch_gateway(monkeypatch, _plans_response([1]))
        r2 = client.post(f"/v1/{project}/volumes/1/plan", json={"to_chapter": 1})
        assert r2.status_code == 200
        conn = _open_conn(project)
        n_beats = conn.execute(
            "SELECT COUNT(*) AS n FROM beats WHERE chapter=1 AND status='planned'"
        ).fetchone()["n"]
        n_cards = conn.execute(
            "SELECT COUNT(*) AS n FROM chapter_cards WHERE chapter=1"
        ).fetchone()["n"]
        conn.close()
        assert n_beats == 3
        assert n_cards == 1

    def test_volume_without_start_422(self, client, project):
        r = client.post(f"/v1/{project}/volumes", json={"volume_no": 1, "title": "无起点卷"})
        assert r.status_code == 201
        r2 = client.post(f"/v1/{project}/volumes/1/plan", json={})
        assert r2.status_code == 422

    def test_patch_card(self, client, project, monkeypatch):
        _create_volume(client, project)
        _patch_gateway(monkeypatch, _plans_response([1]))
        client.post(f"/v1/{project}/volumes/1/plan", json={"to_chapter": 1})

        r = client.patch(f"/v1/{project}/chapter-cards/1", json={"goal": "人工改后的目标"})
        assert r.status_code == 200
        assert r.json()["goal"] == "人工改后的目标"

        nxt = client.get(f"/v1/{project}/pipeline/next").json()
        assert "人工改后的目标" in nxt["suggested_goal"]

    def test_patch_missing_card_404(self, client, project):
        r = client.patch(f"/v1/{project}/chapter-cards/99", json={"goal": "x"})
        assert r.status_code == 404
