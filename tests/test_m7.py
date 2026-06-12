"""M7 测试：diff 式局部修订（锚点补丁 + 全文重写回退）。全部 FakeProvider，无网络。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from novelforge.craft.patch_revise import PatchResult, apply_patches, parse_patches


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
    resp = client.post("/v1/projects", json={"name": "M7测试", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


def _open_conn(project_id):
    from novelforge.app.deps import get_registry
    return get_registry().open_conn(project_id)


# ── apply_patches / parse_patches 单元测试 ────────────────────────────────────

class TestApplyPatches:
    def test_unique_anchor_replaced(self):
        draft = "第一句话。第二句话。第三句话。"
        r = apply_patches(draft, [{"find": "第二句话。", "replace": "改写后的第二句。"}])
        assert r.text == "第一句话。改写后的第二句。第三句话。"
        assert r.applied == 1 and r.failed == 0

    def test_anchor_not_found(self):
        r = apply_patches("一些正文内容在此。", [{"find": "不存在的片段", "replace": "x"}])
        assert r.applied == 0 and r.failed == 1
        assert r.reasons == ["anchor_not_found"]
        assert r.text == "一些正文内容在此。"

    def test_ambiguous_anchor_rejected(self):
        draft = "重复的句子。中间内容。重复的句子。"
        r = apply_patches(draft, [{"find": "重复的句子。", "replace": "x"}])
        assert r.applied == 0 and r.failed == 1
        assert r.reasons == ["anchor_ambiguous"]
        assert r.text == draft

    def test_short_find_rejected(self):
        r = apply_patches("正文内容。", [{"find": "。", "replace": "x"}])
        assert r.failed == 1
        assert r.reasons == ["find_too_short"]

    def test_delete_with_empty_replace(self):
        draft = "保留段。删除这一段冗余内容。保留尾。"
        r = apply_patches(draft, [{"find": "删除这一段冗余内容。", "replace": ""}])
        assert r.text == "保留段。保留尾。"
        assert r.applied == 1

    def test_multiple_patches_mixed(self):
        draft = "句子A。句子B。句子C。"
        r = apply_patches(draft, [
            {"find": "句子A。", "replace": "句子A改。"},
            {"find": "找不到。", "replace": "x"},
            {"find": "句子C。", "replace": "句子C改。"},
        ])
        assert r.text == "句子A改。句子B。句子C改。"
        assert r.applied == 2 and r.failed == 1


class TestParsePatches:
    def test_plain_array(self):
        out = parse_patches('[{"find": "abc", "replace": "def"}]')
        assert out == [{"find": "abc", "replace": "def"}]

    def test_fenced_block(self):
        out = parse_patches('说明文字\n```json\n[{"find": "a", "replace": "b"}]\n```')
        assert len(out) == 1

    def test_truncated_rescued(self):
        full = json.dumps([{"find": "锚点片段", "replace": "替换文本"}], ensure_ascii=False)
        out = parse_patches(full[:-1])    # 去掉收尾 ]
        assert len(out) == 1

    def test_garbage_and_missing_find(self):
        assert parse_patches("不是 JSON") == []
        assert parse_patches('[{"replace": "没有find"}]') == []


# ── 流水线集成 ────────────────────────────────────────────────────────────────

def _unique_draft_body(n: int = 120) -> str:
    """每句唯一，保证补丁锚点可唯一定位。"""
    return "".join(f"第{i}段，陆天继续前行，山路愈发陡峭。" for i in range(n))


def _draft_response(body: str) -> str:
    return (
        f"```draft\n{body}\n```\n"
        "```proposals\n"
        '[{"op":"add","fact_type":"power_rank","entity":"陆天",'
        '"new":{"subject":"陆天","predicate":"境界","object":"炼气一层"},'
        '"valid_from_chapter":1}]\n'
        "```"
    )


def _build_orch(project, *, patch_responses: list[str] | None, patch_revise=True):
    """patch_responses：补丁调用按轮次依次返回（耗尽后复用末项）；
    None 时返回非 JSON（触发回退）。"""
    from novelforge.config import NovelForgeConfig
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway
    from novelforge.control_plane.orchestrator import Orchestrator
    from novelforge.control_plane.skill_registry import SkillRegistry
    from novelforge.skills import register_default_skills

    patch_queue = list(patch_responses or [])

    def factory(messages, model=""):
        user = str(messages[-1].content) if messages else ""
        if "本章任务" in user:
            return _draft_response(_unique_draft_body())
        if "修订补丁任务" in user or "润色补丁任务" in user:
            if patch_responses is None:
                return "这不是 JSON"
            return patch_queue.pop(0) if len(patch_queue) > 1 else patch_queue[0]
        if "一致性问题" in user:          # 全文重写回退
            return "全文重写后的草稿。" * 150
        if "章正文：" in user:
            return "本章摘要。"
        return "[]"

    fake = FakeProvider(factory=factory)
    gw = LLMGateway(fake, BudgetLedger(max_tokens=10_000_000, max_usd=100.0,
                                       max_revise_rounds=100))
    reg = SkillRegistry()
    register_default_skills(reg)
    cfg = NovelForgeConfig(project_id=project)
    cfg.provider.provider = "fake"
    cfg.recall.enable_summaries = False
    cfg.patch_revise = patch_revise
    return Orchestrator(gw, reg, cfg), fake


class TestPatchRevisePipeline:
    def test_patch_applied_preserves_rest(self, client, project):
        """补丁修订：只改锚定段，其余正文逐字保留（不再全文重写）。"""
        patch_r1 = json.dumps([{
            "find": "第10段，陆天继续前行，山路愈发陡峭。",
            "replace": "第10段，陆天放缓脚步，警惕地观察四周。",
        }], ensure_ascii=False)
        patch_r2 = json.dumps([{
            "find": "第20段，陆天继续前行，山路愈发陡峭。",
            "replace": "第20段，山风骤起，陆天握紧了剑柄。",
        }], ensure_ascii=False)
        orch, fake = _build_orch(project, patch_responses=[patch_r1, patch_r2])
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn)
            assert outcome.ok, outcome.error
            row = conn.execute(
                "SELECT detail_json FROM pipeline_run WHERE run_id=?",
                (outcome.run_id,),
            ).fetchone()
        finally:
            conn.close()

        # 两轮补丁段均被替换，相邻段原样保留
        assert "陆天放缓脚步，警惕地观察四周" in outcome.draft_text
        assert "山风骤起，陆天握紧了剑柄" in outcome.draft_text
        assert "第9段，陆天继续前行" in outcome.draft_text
        assert "第11段，陆天继续前行" in outcome.draft_text
        # 没有走全文重写
        assert "全文重写后的草稿" not in outcome.draft_text
        rewrite_calls = [c for c in fake.calls
                         if "一致性问题" in str(c["messages"][-1].content)]
        assert not rewrite_calls
        # 补丁统计落库
        detail = json.loads(row["detail_json"])
        assert detail["patch_stats"]["revise"]["applied"] == 2
        assert detail["patch_stats"]["revise"]["rounds"] == 2

    def test_patch_failure_falls_back_to_full_rewrite(self, client, project):
        """补丁非 JSON → 自动回退全文重写（行为不差于旧版）。"""
        orch, fake = _build_orch(project, patch_responses=None)
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn)
            assert outcome.ok
        finally:
            conn.close()
        assert "全文重写后的草稿" in outcome.draft_text
        rewrite_calls = [c for c in fake.calls
                         if "一致性问题" in str(c["messages"][-1].content)]
        assert rewrite_calls

    def test_anchor_miss_falls_back(self, client, project):
        """补丁锚定全部失败（find 不在草稿中）→ 回退全文重写。"""
        patch = json.dumps([{"find": "草稿里根本没有这句话。", "replace": "x"}],
                           ensure_ascii=False)
        orch, fake = _build_orch(project, patch_responses=[patch])
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn)
            assert outcome.ok
        finally:
            conn.close()
        assert "全文重写后的草稿" in outcome.draft_text

    def test_flag_off_skips_patch_path(self, client, project):
        """patch_revise=False → 不发补丁调用，直接全文重写（旧行为）。"""
        orch, fake = _build_orch(project, patch_responses=["[]"], patch_revise=False)
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn)
            assert outcome.ok
        finally:
            conn.close()
        patch_calls = [c for c in fake.calls
                       if "修订补丁任务" in str(c["messages"][-1].content)]
        assert not patch_calls
        assert "全文重写后的草稿" in outcome.draft_text

    def test_run_detail_exposes_patch_stats(self, client, project):
        patch = json.dumps([{
            "find": "第10段，陆天继续前行，山路愈发陡峭。",
            "replace": "第10段改。",
        }], ensure_ascii=False)
        orch, _ = _build_orch(project, patch_responses=[patch])
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn)
            assert outcome.ok
        finally:
            conn.close()
        r = client.get(f"/v1/{project}/pipeline/runs/{outcome.run_id}")
        assert r.status_code == 200
        stats = r.json()["patch_stats"]
        assert stats["revise"]["applied"] >= 1
        assert r.json()["candidates"] == []   # 单稿 run：候选区不受 patch_stats 影响
