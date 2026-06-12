"""P1 后端核心三项测试：findings 化 / 伏笔结算 / 结算降级。全部 FakeProvider，无网络。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


# ── fixtures（与 test_post_m8 同款）──────────────────────────────────────────

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
    resp = client.post("/v1/projects", json={"name": "P1核心测试", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


def _open_conn(project_id):
    from novelforge.app.deps import get_registry
    return get_registry().open_conn(project_id)


_BEATS_JSON = json.dumps([
    {"beat_type": "setup", "summary": "开场", "value_axis": "平静→波澜"},
    {"beat_type": "turn", "summary": "转折", "value_axis": "守→攻"},
    {"beat_type": "hook", "summary": "章末悬念", "value_axis": "悬念↑"},
], ensure_ascii=False)


def _draft_response(body: str) -> str:
    return (
        f"```draft\n{body}\n```\n"
        "```proposals\n"
        '[{"op":"add","fact_type":"power_rank","entity":"陆天",'
        '"new":{"subject":"陆天","predicate":"境界","object":"炼气一层"},'
        '"valid_from_chapter":1}]\n'
        "```"
    )


def _build_orch(project, factory, *, n_candidates=1, quality=False, settle=False):
    from novelforge.config import NovelForgeConfig
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway
    from novelforge.control_plane.orchestrator import Orchestrator
    from novelforge.control_plane.skill_registry import SkillRegistry
    from novelforge.skills import register_default_skills

    fake = FakeProvider(factory=factory)
    gw = LLMGateway(fake, BudgetLedger(max_tokens=10_000_000, max_usd=100.0,
                                       max_revise_rounds=100))
    reg = SkillRegistry()
    register_default_skills(reg)
    cfg = NovelForgeConfig(project_id=project)
    cfg.provider.provider = "fake"
    cfg.candidates.n_candidates = n_candidates
    cfg.quality.enabled = quality
    cfg.recall.enable_summaries = False
    cfg.settle.enabled = settle
    return Orchestrator(gw, reg, cfg), fake


# ── #8 findings 归一化（纯函数，零 LLM）─────────────────────────────────────

DRAFT = "陆天踏入山门，长老瞳孔骤缩。他说：你竟已是炼气三层。陆天微微一笑。"


class TestNormalizeFindings:
    def test_llm_finding_without_evidence_dropped(self):
        from novelforge.craft.findings import normalize_findings
        raw = [{"issue": "能力异常", "evidence": "这句话不在草稿里", "severity": "block"},
               {"issue": "境界跳级", "evidence": "你竟已是炼气三层", "severity": "block"}]
        out = normalize_findings(raw, DRAFT, "llm_soft")
        assert len(out) == 1 and out[0]["issue"] == "境界跳级"

    def test_evidence_whitespace_normalized(self):
        from novelforge.craft.findings import normalize_findings
        raw = [{"issue": "x", "evidence": "你竟已是　炼气三层", "severity": "warn"}]
        assert len(normalize_findings(raw, DRAFT, "llm_soft")) == 1

    def test_legacy_field_names_mapped(self):
        from novelforge.craft.findings import normalize_findings
        raw = [{"desc": "旧字段", "span": "陆天踏入山门", "subclass": "2.3-能力波动",
                "severity": "block"}]
        out = normalize_findings(raw, DRAFT, "llm_soft")
        assert out[0]["issue"] == "旧字段"
        assert out[0]["evidence"] == "陆天踏入山门"
        assert out[0]["category"] == "2.3-能力波动"

    def test_malformed_fields_lenient(self):
        from novelforge.craft.findings import normalize_findings
        raw = [{"issue": "严重度非法", "evidence": "陆天踏入山门", "severity": "fatal",
                "repair_scope": "全局"},
               "不是字典", {"evidence": "陆天踏入山门"}]
        out = normalize_findings(raw, DRAFT, "llm_soft")
        assert len(out) == 1
        assert out[0]["severity"] == "warn" and out[0]["repair_scope"] == "local"

    def test_validator_source_no_evidence_required(self):
        from novelforge.craft.findings import normalize_findings
        out = normalize_findings([{"desc": "境界回退", "severity": "block"}], DRAFT, "validator")
        assert len(out) == 1 and out[0]["severity"] == "block"

    def test_issues_str_contains_evidence_and_fix(self):
        from novelforge.craft.findings import findings_to_issues_str
        s = findings_to_issues_str([{"category": "craft.hook", "issue": "缺钩子",
                                     "evidence": "陆天微微一笑", "fix": "加悬念句"}])
        assert "缺钩子" in s and "陆天微微一笑" in s and "加悬念句" in s

    def test_issues_str_legacy_keys_fallback(self):
        from novelforge.craft.findings import findings_to_issues_str
        s = findings_to_issues_str([{"check": "hook", "detail": "旧格式问题", "span": ""}])
        assert "旧格式问题" in s


# ── #8 check skill 输出 findings 字段 ────────────────────────────────────────

class TestFindingsInChecks:
    def test_soft_finding_without_evidence_dropped_in_pipeline(self, client, project):
        """软检查报了无证据问题 → 管线内被丢弃，不触发 revise。"""
        body = "平静叙事正文。" * 200

        def factory(messages, model="", temperature=1.0):
            user = str(messages[-1].content) if messages else ""
            if "规划任务" in user:
                return _BEATS_JSON
            if "本章任务" in user:
                return _draft_response(body)
            if "草稿：" in user:   # continuity 软检查
                return json.dumps([{"category": "2.3", "severity": "block",
                                    "issue": "捏造的问题", "evidence": "草稿里没有这句话",
                                    "repair_scope": "local"}], ensure_ascii=False)
            return "[]"

        orch, fake = _build_orch(project, factory)
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn, chapter_goal="测试")
        finally:
            conn.close()
        assert outcome.ok
        # 无证据 block 被丢弃 → 没有任何修订调用
        revise_calls = [c for c in fake.calls
                        if "一致性问题" in str(c["messages"][-1].content)
                        or "修订补丁任务" in str(c["messages"][-1].content)]
        assert revise_calls == []

    def test_craft_issue_dict_carries_new_fields(self):
        from novelforge.skills.craft_check_skill import CraftIssue, _issue_dict
        d = _issue_dict(CraftIssue(check="hook", severity="block", detail="无钩子"))
        assert d["check"] == "hook" and d["detail"] == "无钩子"          # 旧字段保留
        assert d["category"] == "craft.hook" and d["issue"] == "无钩子"  # 新字段
        assert d["repair_scope"] == "local" and d["source"] == "craft"


# ── #8/P0#2 repair_scope 修订路由 ────────────────────────────────────────────

class TestRepairScopeRouting:
    """structural → 直接全文重写；全 local → 锚点补丁先行（现有回退保留）。"""

    def _factory(self, scope: str, marker: dict):
        body = "陆天踏入山门，他的境界是炼气三层。" + "平铺叙事。" * 150

        def factory(messages, model="", temperature=1.0):
            user = str(messages[-1].content) if messages else ""
            if "规划任务" in user:
                return _BEATS_JSON
            if "本章任务" in user:
                return _draft_response(body)
            if "草稿：" in user:
                if "炼气三层" in user and not marker.get("reported"):
                    marker["reported"] = True
                    return json.dumps([{"category": "2.3", "severity": "block",
                                        "issue": "境界跳级", "evidence": "他的境界是炼气三层",
                                        "fix": "改为炼气一层", "repair_scope": scope}],
                                      ensure_ascii=False)
                return "[]"
            if "修订补丁任务" in user:
                return json.dumps([{"find": "他的境界是炼气三层",
                                    "replace": "他的境界是炼气一层"}], ensure_ascii=False)
            if "一致性问题" in user:
                return "重写后的正文。" * 200
            return "[]"
        return factory

    def test_structural_skips_patch_goes_rewrite(self, client, project):
        marker = {}
        orch, fake = _build_orch(project, self._factory("structural", marker))
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn, chapter_goal="测试")
        finally:
            conn.close()
        assert outcome.ok
        users = [str(c["messages"][-1].content) for c in fake.calls]
        assert not any("修订补丁任务" in u for u in users), "structural 不应走补丁"
        assert any("一致性问题" in u for u in users), "structural 应直接全文重写"

    def test_local_tries_patch_first(self, client, project):
        marker = {}
        orch, fake = _build_orch(project, self._factory("local", marker))
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn, chapter_goal="测试")
        finally:
            conn.close()
        assert outcome.ok
        users = [str(c["messages"][-1].content) for c in fake.calls]
        patch_calls = [u for u in users if "修订补丁任务" in u]
        assert patch_calls, "local 应锚点补丁先行"
        assert "原文：「他的境界是炼气三层」" in patch_calls[0], "issues_str 应携带 evidence"
        assert "建议：改为炼气一层" in patch_calls[0], "issues_str 应携带 fix"
