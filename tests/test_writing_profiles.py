"""P0 writing-profile catalog, persistence, versioning and compatibility."""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_DATA", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NOVELFORGE_API_KEY", raising=False)
    import novelforge.app.deps as deps_mod
    deps_mod._registry = None
    from novelforge.app.main import app
    with TestClient(app) as value:
        yield value
    deps_mod._registry = None


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _english_payload(**overrides):
    value = {
        "name": "The Glass Stair",
        "content_language": "en-US",
        "target_market": "global_english",
        "publication_format": "web_serial",
        "primary_genre": "fantasy",
        "story_tags": ["progression_fantasy"],
        "audience_rating": "young_adult",
        "content_warnings": ["violence"],
        "writing_profile_id": "en_progression_serial_v1",
        "setting": {
            "genre": "fantasy",
            "premise": "A courier climbs a living tower to recover her stolen name.",
            "protagonist": "Mara, a cautious courier who learns through deliberate risks.",
            "main_conflict": "Every floor trades power for a memory she cannot replace.",
        },
    }
    value.update(overrides)
    return value


def test_backend_catalog_is_filtered_and_versioned(client):
    profiles = client.get(
        "/v1/catalogs/writing-profiles?language=en-US&format=web_serial"
    )
    assert profiles.status_code == 200
    body = profiles.json()
    assert body["catalog_version"] == "1.0.0"
    assert len(body["items"]) == 8
    assert {item["id"] for item in body["items"]} >= {
        "en_progression_serial_v1", "en_cozy_fantasy_v1",
        "en_mystery_thriller_v1",
    }
    assert all(item["language"] == "en-US" for item in body["items"])

    genres = client.get(
        "/v1/catalogs/genres?language=en-US&market=global_english"
    ).json()["items"]
    assert "fantasy" in {item["id"] for item in genres}
    assert "xuanhuan" not in {item["id"] for item in genres}


def test_legacy_create_defaults_to_chinese_profile(client):
    response = client.post("/v1/projects", json={"name": "旧客户端", "genre": "xuanhuan"})
    assert response.status_code == 201, response.text
    project = response.json()
    assert project["content_language"] == "zh-CN"
    assert project["target_market"] == "cn_web"
    assert project["writing_profile_id"] == "cn_web_novel_v1"
    assert project["writing_profile_version"] == 1


def test_english_create_roundtrips_profile_and_append_only_snapshot(client):
    response = client.post("/v1/projects", json=_english_payload())
    assert response.status_code == 201, response.text
    project = response.json()
    assert project["genre"] == "fantasy"
    assert project["content_language"] == "en-US"
    assert project["story_tags"] == ["progression_fantasy"]
    assert project["writing_profile_summary"]["display_name"] == "Progression Fantasy Serial"

    detail = client.get(f"/v1/projects/{project['project_id']}").json()
    assert detail["writing_profile_id"] == "en_progression_serial_v1"
    envelope = client.get(f"/v1/{project['project_id']}/writing-profile")
    assert envelope.status_code == 200, envelope.text
    current = envelope.json()["current"]
    history = envelope.json()["history"]
    assert current["quality_rules"]["chapter_word_range"] == [1500, 2500]
    assert current["cache_key"].startswith("en-US:en_progression_serial_v1:1:")
    assert [item["revision"] for item in history] == [1]

    conn = _connect(project["db_path"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM writing_profile_versions").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE writing_profile_versions SET change_reason='mutated'")
    finally:
        conn.rollback()
        conn.close()


def test_profile_patch_versions_future_generation_and_guards_history(client):
    project = client.post("/v1/projects", json=_english_payload()).json()
    pid = project["project_id"]
    patched = client.patch(f"/v1/{pid}/writing-profile", json={
        "writing_profile_id": "en_cozy_fantasy_v1",
        "story_tags": ["cozy"],
        "change_reason": "Shift the reader promise before drafting",
    })
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["current"]["writing_profile_id"] == "en_cozy_fantasy_v1"
    assert body["current"]["writing_profile_version"] == 2
    assert [item["revision"] for item in body["history"]] == [2, 1]

    conn = _connect(project["db_path"])
    try:
        conn.execute(
            "INSERT INTO draft_index(id,chapter,revision_round,file_path,sha256,word_count,status)"
            " VALUES('draft_existing',1,0,'l0/ch1.txt','sha',1500,'draft')"
        )
        conn.commit()
    finally:
        conn.close()

    language_change = client.patch(f"/v1/{pid}/writing-profile", json={
        "content_language": "zh-CN",
        "target_market": "cn_web",
        "writing_profile_id": "cn_web_novel_v1",
    })
    assert language_change.status_code == 409
    assert language_change.json()["error"]["code"] == "content_language_migration_required"

    high_risk = client.patch(f"/v1/{pid}/writing-profile", json={
        "writing_profile_overrides": {"pov": "first_person"},
    })
    assert high_risk.status_code == 409
    assert high_risk.json()["error"]["code"] == "high_risk_confirmation_required"

    confirmed = client.patch(f"/v1/{pid}/writing-profile", json={
        "writing_profile_overrides": {"pov": "first_person"},
        "confirm_high_risk": True,
    })
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["current"]["effective_from_chapter"] == 2


def test_profile_resolution_honors_scheduled_effective_chapter(client):
    from novelforge.writing_profiles import get_project_writing_profile

    project = client.post("/v1/projects", json=_english_payload()).json()
    patched = client.patch(
        f"/v1/{project['project_id']}/writing-profile",
        json={
            "writing_profile_id": "en_cozy_fantasy_v1",
            "story_tags": ["cozy"],
            "effective_from_chapter": 3,
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["current"]["writing_profile_id"] == "en_cozy_fantasy_v1"

    conn = _connect(project["db_path"])
    try:
        before = get_project_writing_profile(conn, chapter=2)
        effective = get_project_writing_profile(conn, chapter=3)
    finally:
        conn.close()

    assert before["writing_profile_id"] == "en_progression_serial_v1"
    assert before["writing_profile_version"] == 1
    assert effective["writing_profile_id"] == "en_cozy_fantasy_v1"
    assert effective["writing_profile_version"] == 2
    assert before["cache_key"] != effective["cache_key"]


def test_stored_profile_snapshot_is_independent_of_current_catalog(
    client, monkeypatch,
):
    import novelforge.writing_profiles as profiles

    project = client.post("/v1/projects", json=_english_payload()).json()
    catalog_profile = profiles._PROFILE_BY_ID["en_progression_serial_v1"]
    monkeypatch.setitem(catalog_profile["prompt_rules"], "pace", "slow_burn")
    monkeypatch.setitem(catalog_profile, "prompt_version", "future-catalog")

    conn = _connect(project["db_path"])
    try:
        historical = profiles.get_project_writing_profile(conn, chapter=1)
        current = profiles.get_project_writing_profile(conn)
    finally:
        conn.close()

    assert historical["prompt_rules"]["pace"] == "fast"
    assert current["prompt_rules"]["pace"] == "fast"
    assert historical["prompt_version"] == "1.0.0"


def test_newer_revision_supersedes_an_older_later_schedule(client):
    from novelforge.writing_profiles import get_project_writing_profile

    project = client.post("/v1/projects", json=_english_payload()).json()
    pid = project["project_id"]
    first = client.patch(f"/v1/{pid}/writing-profile", json={
        "writing_profile_id": "en_cozy_fantasy_v1",
        "story_tags": ["cozy"],
        "effective_from_chapter": 20,
    })
    assert first.status_code == 200, first.text
    second = client.patch(f"/v1/{pid}/writing-profile", json={
        "writing_profile_id": "en_mystery_thriller_v1",
        "story_tags": ["mystery"],
        "effective_from_chapter": 10,
    })
    assert second.status_code == 200, second.text

    conn = _connect(project["db_path"])
    try:
        at_twenty = get_project_writing_profile(conn, chapter=20)
    finally:
        conn.close()
    assert at_twenty["writing_profile_id"] == "en_mystery_thriller_v1"
    assert at_twenty["writing_profile_version"] == 3


def test_profile_update_rolls_back_when_history_cannot_be_written(client):
    from novelforge.db.connection import get_meta
    from novelforge.writing_profiles import update_project_writing_profile

    project = client.post("/v1/projects", json=_english_payload()).json()
    conn = _connect(project["db_path"])
    try:
        conn.execute("DROP TABLE writing_profile_versions")
        conn.commit()
        with pytest.raises(sqlite3.OperationalError, match="writing_profile_versions"):
            update_project_writing_profile(
                conn, {"story_tags": ["cozy"]},
            )
        assert get_meta(conn, "writing_profile_version") == "1"
        assert json.loads(get_meta(conn, "story_tags_json")) == [
            "progression_fantasy"
        ]
    finally:
        conn.close()


def test_empty_profile_patch_does_not_create_a_revision(client):
    project = client.post("/v1/projects", json=_english_payload()).json()
    response = client.patch(
        f"/v1/{project['project_id']}/writing-profile", json={},
    )

    assert response.status_code == 200, response.text
    assert response.json()["current"]["writing_profile_version"] == 1
    assert len(response.json()["history"]) == 1


def test_english_primary_genre_must_come_from_compatible_catalog(client):
    response = client.post(
        "/v1/projects",
        json=_english_payload(primary_genre="xuanhuan"),
    )

    assert response.status_code == 422
    assert "primary genre" in response.text


def test_project_read_does_not_mask_database_corruption_as_chinese_defaults(client):
    project = client.post("/v1/projects", json=_english_payload()).json()
    conn = _connect(project["db_path"])
    try:
        conn.execute("DROP TABLE draft_index")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(sqlite3.OperationalError, match="draft_index"):
        client.get(f"/v1/projects/{project['project_id']}")


def test_profile_reads_do_not_mask_a_broken_history_schema(client):
    from novelforge.writing_profiles import (
        get_project_writing_profile,
        list_project_profile_history,
    )

    project = client.post("/v1/projects", json=_english_payload()).json()
    conn = _connect(project["db_path"])
    try:
        conn.execute(
            "ALTER TABLE writing_profile_versions "
            "RENAME COLUMN snapshot_json TO broken_snapshot_json"
        )
        conn.commit()
        with pytest.raises(sqlite3.OperationalError, match="snapshot_json"):
            get_project_writing_profile(conn)
        with pytest.raises(sqlite3.OperationalError, match="snapshot_json"):
            get_project_writing_profile(conn, chapter=1)
        with pytest.raises(sqlite3.OperationalError, match="snapshot_json"):
            list_project_profile_history(conn)
    finally:
        conn.close()


def test_invalid_profile_language_is_rejected_before_project_creation(client, tmp_path):
    response = client.post("/v1/projects", json=_english_payload(
        writing_profile_id="cn_web_novel_v1",
    ))
    assert response.status_code == 422
    registry = tmp_path / "projects.json"
    if registry.exists():
        assert json.loads(registry.read_text(encoding="utf-8"))["projects"] == []


def test_english_suggest_fallback_is_english_and_not_persisted_as_setting(client):
    response = client.post("/v1/projects/setup/suggest", json={
        "content_language": "en-US",
        "target_market": "global_english",
        "publication_format": "web_serial",
        "primary_genre": "fantasy",
        "writing_profile_id": "en_progression_serial_v1",
        "genre": "fantasy",
        "n": 1,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    if body["degraded"]:
        candidate = body["candidates"][0]
        assert candidate["premise"].startswith("[Example]")
        created = client.post("/v1/projects", json=_english_payload(setting=candidate))
        assert created.status_code == 201
        conn = _connect(created.json()["db_path"])
        try:
            assert conn.execute(
                "SELECT 1 FROM meta_kv WHERE key='premise'"
            ).fetchone() is None
        finally:
            conn.close()
