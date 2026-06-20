"""情节设定（手填 / 生成）测试。

MVP-1：建项目时整套设定散件落 meta_kv，力量阶梯落 power_ranks 表，response 回显。
MVP-2/3：SettingGenSkill + POST /v1/projects/setup/suggest（生成候选 / reroll / 锁定再生成）。

全程 FakeProvider（无 API key），无网络。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_DATA", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("NOVELFORGE_API_KEY", raising=False)
    import novelforge.app.deps as deps_mod
    import novelforge.app.autopilot_manager as ap_mod
    deps_mod._registry = None
    ap_mod._manager = None
    yield tmp_path
    deps_mod._registry = None
    ap_mod._manager = None


@pytest.fixture
def client(tmp_data):
    from novelforge.app.main import app
    with TestClient(app) as c:
        yield c


def _open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ── MVP-1：手填设定落库 ────────────────────────────────────────────────────────

class TestCreateWithSetting:
    def test_setting_written_to_meta_kv(self, client):
        """整套设定散件写入 meta_kv，可逐项读回。"""
        from novelforge.db.connection import get_meta

        resp = client.post("/v1/projects", json={
            "name": "万古第一剑修",
            "genre": "xianxia",
            "setting": {
                "genre": "xianxia",
                "premise": "废柴觉醒万古剑魂，逆袭成仙。",
                "world_setting": "九州大陆，灵气复苏。",
                "power_system": "剑道九重",
                "protagonist": "林尘，被废丹田的世家少年。",
                "main_conflict": "与压迫家族及仙门的对抗。",
                "opening_hook": "祭祖大典上，废脉少年剑鸣震天。",
                "target_audience": "男频玄幻读者",
                "tone": "热血爽快",
            },
        })
        assert resp.status_code == 201, resp.text
        body = resp.json()
        conn = _open_db(body["db_path"])
        try:
            assert get_meta(conn, "premise") == "废柴觉醒万古剑魂，逆袭成仙。"
            assert get_meta(conn, "world_setting") == "九州大陆，灵气复苏。"
            assert get_meta(conn, "power_system") == "剑道九重"
            assert get_meta(conn, "protagonist") == "林尘，被废丹田的世家少年。"
            assert get_meta(conn, "main_conflict") == "与压迫家族及仙门的对抗。"
            assert get_meta(conn, "opening_hook") == "祭祖大典上，废脉少年剑鸣震天。"
            assert get_meta(conn, "target_audience") == "男频玄幻读者"
            assert get_meta(conn, "tone") == "热血爽快"
            # genre 单一真相源在 ProjectMeta，不双写 meta_kv
            assert get_meta(conn, "genre") is None
        finally:
            conn.close()

    def test_power_ranks_written_to_table(self, client):
        """有序力量阶梯写入 power_ranks 表，rank_order 按列表顺序严格递增。"""
        resp = client.post("/v1/projects", json={
            "name": "炼气测试",
            "genre": "xianxia",
            "setting": {
                "genre": "xianxia",
                "premise": "p",
                "protagonist": "主角",
                "main_conflict": "冲突",
                "power_system": "炼气体系",
                "power_ranks": ["炼气", "筑基", "金丹", "元婴"],
            },
        })
        assert resp.status_code == 201, resp.text
        conn = _open_db(resp.json()["db_path"])
        try:
            rows = conn.execute(
                "SELECT system_name, rank_name, rank_order FROM power_ranks "
                "ORDER BY rank_order"
            ).fetchall()
            assert [r["rank_name"] for r in rows] == ["炼气", "筑基", "金丹", "元婴"]
            assert all(r["system_name"] == "炼气体系" for r in rows)
            orders = [r["rank_order"] for r in rows]
            assert orders == sorted(orders)
            assert len(set(orders)) == len(orders)  # 唯一
        finally:
            conn.close()

    def test_response_echoes_power_system_and_premise(self, client):
        resp = client.post("/v1/projects", json={
            "name": "回显测试",
            "genre": "xuanhuan",
            "setting": {"genre": "xuanhuan", "power_system": "斗气",
                        "premise": "少年崛起", "protagonist": "萧炎",
                        "main_conflict": "家族复兴"},
        })
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["power_system"] == "斗气"
        assert body["premise"] == "少年崛起"

    def test_setting_genre_wins_when_explicit(self, client):
        """候选卡显式带 genre 时，ProjectMeta.genre 不能继续使用旧顶层值。"""
        resp = client.post("/v1/projects", json={
            "name": "题材候选",
            "genre": "xuanhuan",
            "setting": {"genre": "kehuan", "premise": "星舰修真",
                        "protagonist": "林星", "main_conflict": "舰队围猎"},
        })
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["genre"] == "kehuan"

    def test_list_and_get_echo_setting(self, client):
        """回显字段在 GET 列表/详情也要从 meta_kv 读回（不只是 create 响应）。"""
        resp = client.post("/v1/projects", json={
            "name": "回显接口", "genre": "xuanhuan",
            "setting": {"genre": "xuanhuan", "power_system": "斗气",
                        "premise": "少年崛起", "protagonist": "萧炎",
                        "main_conflict": "家族复兴"},
        })
        pid = resp.json()["project_id"]

        g = client.get(f"/v1/projects/{pid}")
        assert g.status_code == 200
        assert g.json()["power_system"] == "斗气"
        assert g.json()["premise"] == "少年崛起"

        lst = client.get("/v1/projects").json()
        item = next(p for p in lst if p["project_id"] == pid)
        assert item["power_system"] == "斗气"
        assert item["premise"] == "少年崛起"

    def test_backward_compat_power_system_only(self, client):
        """旧调用方只传 power_system（无 setting）仍写入 meta_kv 并回显。"""
        from novelforge.db.connection import get_meta

        resp = client.post("/v1/projects", json={
            "name": "旧接口",
            "genre": "xuanhuan",
            "power_system": "斗之气",
        })
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["power_system"] == "斗之气"
        conn = _open_db(body["db_path"])
        try:
            assert get_meta(conn, "power_system") == "斗之气"
        finally:
            conn.close()

    def test_no_setting_creates_clean_project(self, client):
        """完全不带设定也能建项目，无 power_ranks 行，回显字段为 None。"""
        resp = client.post("/v1/projects", json={"name": "空白", "genre": "dushi"})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body.get("power_system") is None
        assert body.get("premise") is None
        conn = _open_db(body["db_path"])
        try:
            n = conn.execute("SELECT COUNT(*) AS c FROM power_ranks").fetchone()["c"]
            assert n == 0
        finally:
            conn.close()


# ── MVP-2：生成候选端点 ────────────────────────────────────────────────────────

class TestSuggestEndpoint:
    def test_degraded_returns_candidates(self, client):
        """无 API key → degraded=True，仍返回 ≥1 个候选，不报 500。"""
        resp = client.post("/v1/projects/setup/suggest", json={
            "genre": "xianxia", "n": 3,
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["degraded"] is True
        assert len(body["candidates"]) >= 1
        from novelforge.app.models import GENRE_VALUES
        assert all(c["genre"] in GENRE_VALUES for c in body["candidates"])

    def test_n_too_large_rejected(self, client):
        resp = client.post("/v1/projects/setup/suggest", json={"genre": "xianxia", "n": 6})
        assert resp.status_code == 422

    def test_n_too_small_rejected(self, client):
        resp = client.post("/v1/projects/setup/suggest", json={"genre": "xianxia", "n": 0})
        assert resp.status_code == 422

    def test_suggest_not_captured_by_project_id_route(self, client):
        """/setup/suggest 不被 /{project_id} 动态路由误捕获（应 200 而非 404/405）。"""
        resp = client.post("/v1/projects/setup/suggest", json={"genre": "dushi"})
        assert resp.status_code == 200, resp.text
