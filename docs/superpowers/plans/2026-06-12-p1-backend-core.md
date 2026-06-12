# P1 后端核心三项 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地调研 P1 的三项后端核心：#8 审稿 findings 化（含 P0#2 repair_scope 路由）、#6 伏笔 mention/advance 结算 + 确定性仲裁、#11 结算降级不阻塞。

**Architecture:** findings 先行（#6/#11 复用其产物）；伏笔结算为 COMMIT 后新 SETTLE 子步；generate_chapter 重排为「正文先落袋 → 结算块降级保护」。Spec：`docs/superpowers/specs/2026-06-12-p1-backend-core-design.md`。

**Tech Stack:** Python/FastAPI/SQLite（迁移 registry 模式）、FakeProvider 测试、React/TS 前端最小集。

**测试命令：** `.venv/bin/python -m pytest tests/ -q`（全量）；前端 `cd web && npm run build`。

**执行注意：**
- 退化决策（spec §三 的细化）：结算块拆为「步骤 A：canon 结算（candidates→dedup→conflict→gate），失败清理本次创建的候选行后**重试一次**」+「步骤 B-E：pacing/摘要/伏笔翻转/伏笔结算，各自单次 + 单独降级」。理由：A 整块重放靠"清理候选行 + dedup 对已提交事实的 merge 判定"保证不重复提交；B-E 天然幂等或可容忍缺失。
- 所有既有测试必须保持绿：workspace 键名（continuity_issues/craft_issues）与元素旧字段（check/detail/span/desc）一律保留，只增不删。

---

## Task 0: 开分支

- [ ] **Step 1:** `git checkout -b feat/p1-core`（main 干净后执行；绝不在 main 上直接开发）

---

## Task 1: 迁移 v12（foreshadow 列 + foreshadow_log 表）

**Files:**
- Create: `novelforge/db/migrations/v12.py`
- Modify: `novelforge/db/migrations/__init__.py`（import 列表加 v12）
- Modify: `novelforge/db/schema.sql`（foreshadow 表定义 + 新表，基线同步——上批 v11 的教训：新库走 schema.sql 不走迁移链）
- Modify: `novelforge/__init__.py`（`SCHEMA_VERSION = "12"`）
- Test: `tests/test_migrations.py`（既有 migrate-from-v4 测试追加断言）

- [ ] **Step 1: 写失败测试** —— 在 `tests/test_migrations.py` 既有「从 v4 迁移」测试函数末尾追加：

```python
    # v12: foreshadow 结算列 + foreshadow_log 审计表
    cols = {r[1] for r in conn.execute("PRAGMA table_info(foreshadow)").fetchall()}
    assert {"last_mentioned_chapter", "advance_count",
            "last_advanced_chapter", "origin"} <= cols
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "foreshadow_log" in tables
```

同文件如有「新库直接 schema.sql」类测试（test_post_m8 用项目创建路径验证过 v11），在本文件新增：

```python
def test_v12_fresh_db_has_settle_columns(tmp_path):
    """新库从 schema.sql 基线创建，必须直接含 v12 列/表（不经迁移链）。"""
    import sqlite3
    from novelforge.db import init_db   # 若实际入口不同，按 db/__init__.py 中建库函数调整
    conn = sqlite3.connect(tmp_path / "fresh.db")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(foreshadow)").fetchall()}
    assert {"last_mentioned_chapter", "advance_count", "last_advanced_chapter", "origin"} <= cols
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='foreshadow_log'").fetchone()
```

（执行时先看 `novelforge/db/__init__.py` 确认建库入口名，照既有测试的建库方式写。）

- [ ] **Step 2:** `.venv/bin/python -m pytest tests/test_migrations.py -q` → 预期 FAIL（列不存在）

- [ ] **Step 3: 实现** —— `novelforge/db/migrations/v12.py`：

```python
"""Migration v11 → v12: foreshadow 结算列（mention/advance 二分）+ foreshadow_log 审计表（P1#6）。"""
from __future__ import annotations
import sqlite3
from . import register, column_exists


@register("12")
def migrate_v12(conn: sqlite3.Connection) -> None:
    if not column_exists(conn, "foreshadow", "last_mentioned_chapter"):
        conn.execute("ALTER TABLE foreshadow ADD COLUMN last_mentioned_chapter INTEGER")
    if not column_exists(conn, "foreshadow", "advance_count"):
        conn.execute("ALTER TABLE foreshadow ADD COLUMN advance_count INTEGER NOT NULL DEFAULT 0")
    if not column_exists(conn, "foreshadow", "last_advanced_chapter"):
        conn.execute("ALTER TABLE foreshadow ADD COLUMN last_advanced_chapter INTEGER")
    if not column_exists(conn, "foreshadow", "origin"):
        conn.execute("ALTER TABLE foreshadow ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS foreshadow_log (
            id            TEXT PRIMARY KEY,
            foreshadow_id TEXT NOT NULL REFERENCES foreshadow(id),
            chapter       INTEGER NOT NULL,
            action        TEXT NOT NULL CHECK(action IN ('plant','mention','advance','payoff')),
            evidence      TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fslog_fs ON foreshadow_log(foreshadow_id, chapter)")
```

`migrations/__init__.py`：仿 v11 在 import 链尾追加 `from . import v12  # noqa`（看现有写法照抄）。
`novelforge/__init__.py`：`SCHEMA_VERSION = "12"`。
`schema.sql`：foreshadow 表定义中 `fact_id TEXT,` 之后插入四列（带同样默认值），文件 7) craft layer 段落 foreshadow 索引之后加 foreshadow_log 表 + 索引（与迁移 SQL 逐字一致）。

- [ ] **Step 4:** `.venv/bin/python -m pytest tests/test_migrations.py -q` → PASS
- [ ] **Step 5:** `git add -A && git commit -m "feat(P1#6): 迁移 v12——foreshadow 结算列 + foreshadow_log 审计表"`

---

## Task 2: `craft/findings.py`（纯函数，零 LLM）

**Files:**
- Create: `novelforge/craft/findings.py`
- Test: `tests/test_p1_core.py`（新文件，本任务起累积）

- [ ] **Step 1: 写失败测试** —— `tests/test_p1_core.py`：

```python
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


# ── #8 findings 归一化 ────────────────────────────────────────────────────────

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
```

- [ ] **Step 2:** `.venv/bin/python -m pytest tests/test_p1_core.py -q` → FAIL（模块不存在）

- [ ] **Step 3: 实现** —— `novelforge/craft/findings.py`：

```python
"""Finding 统一结构（P1#8，oh-story Findings Schema + inkos repair_scope）。

{"severity": "block|warn", "category": "...", "evidence": "草稿原文片段",
 "issue": "...", "fix": "...", "repair_scope": "local|structural", "source": "..."}

LLM 来源（llm_soft/craft_llm）的 finding：evidence 必须是草稿子串（空白归一后），
否则整条丢弃——「无证据不输出」。validator/craft 确定性来源不强制 evidence。
"""
from __future__ import annotations

import re

_VALID_SEVERITY = {"block", "warn"}
_VALID_SCOPE = {"local", "structural"}
_LLM_SOURCES = {"llm_soft", "craft_llm"}


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def normalize_findings(raw: list, draft_text: str, source: str) -> list[dict]:
    """逐字段宽容归一：畸形字段丢该条不崩整轮；旧字段名兼容映射。"""
    draft_norm = _norm_ws(draft_text)
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        issue = str(item.get("issue") or item.get("desc") or item.get("detail") or "").strip()
        if not issue:
            continue
        evidence = str(item.get("evidence") or item.get("span") or "").strip()
        if source in _LLM_SOURCES and (not evidence or _norm_ws(evidence) not in draft_norm):
            continue   # 无证据不输出
        severity = item.get("severity")
        if severity not in _VALID_SEVERITY:
            severity = "warn"
        scope = item.get("repair_scope")
        if scope not in _VALID_SCOPE:
            scope = "local"
        category = str(item.get("category") or item.get("subclass")
                       or item.get("check") or "general")
        out.append({
            "severity": severity, "category": category,
            "evidence": evidence[:300], "issue": issue[:500],
            "fix": str(item.get("fix") or "")[:200],
            "repair_scope": scope, "source": source,
        })
    return out


def findings_to_issues_str(findings: list[dict]) -> str:
    """补丁/重写 prompt 的问题清单：issue + 原文证据 + 修改建议三行体。

    evidence 是锚点补丁 find 字段的天然锚点；兼容旧 issue 形（check/detail/span/desc）。
    """
    lines: list[str] = []
    for f in findings:
        cat = f.get("category") or f.get("check") or f.get("subclass") or "?"
        issue = f.get("issue") or f.get("desc") or f.get("detail") or str(f)
        lines.append(f"- [{cat}] {issue}")
        ev = f.get("evidence") or f.get("span")
        if ev:
            lines.append(f"  原文：「{ev}」")
        if f.get("fix"):
            lines.append(f"  建议：{f['fix']}")
    return "\n".join(lines)
```

- [ ] **Step 4:** `.venv/bin/python -m pytest tests/test_p1_core.py -q` → PASS
- [ ] **Step 5:** `git add -A && git commit -m "feat(P1#8): craft/findings——统一 Finding 结构 + 证据强制 + 宽容解析"`

---

## Task 3: findings 接入两个 check skill

**Files:**
- Modify: `novelforge/skills/continuity_check_skill.py`（`_SOFT_SYSTEM` 输出字段 + `_run_soft_check` 过 normalize + 硬 validator 输出也过 normalize）
- Modify: `novelforge/skills/craft_check_skill.py`（`_issue_dict` 增新字段；`_FLAT_SYSTEM` 增 evidence/fix；LLM 子检查过 normalize）
- Test: `tests/test_p1_core.py`

- [ ] **Step 1: 写失败测试**（追加到 test_p1_core.py）：

```python
# ── #8 check skill 输出 findings 字段 ────────────────────────────────────────

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


def _draft_response(body: str) -> str:
    return (
        f"```draft\n{body}\n```\n"
        "```proposals\n"
        '[{"op":"add","fact_type":"power_rank","entity":"陆天",'
        '"new":{"subject":"陆天","predicate":"境界","object":"炼气一层"},'
        '"valid_from_chapter":1}]\n'
        "```"
    )


class TestFindingsInChecks:
    def test_soft_finding_without_evidence_dropped_in_pipeline(self, client, project):
        """软检查报了无证据问题 → 管线内被丢弃，不触发 revise。"""
        body = "平静叙事正文。" * 200

        def factory(messages, model="", temperature=1.0):
            user = str(messages[-1].content) if messages else ""
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
```

- [ ] **Step 2:** 运行 → FAIL（cfg.settle 不存在 → 先在本任务一并加 SettleConfig 占位，见 Step 3；_issue_dict 无新字段）

- [ ] **Step 3: 实现**

`novelforge/config.py` 加（放 QualityConfig 之后）：

```python
@dataclass
class SettleConfig:
    """P1#6: 章末伏笔结算（mention/advance/payoff 判定 + 新伏笔确定性仲裁）。"""
    enabled: bool = True                 # 默认开（用户确认）；FAST 档单章成本可忽略
    tier: str = "fast"                   # 结算 LLM 档位
    max_new_hooks: int = 2               # 每章自动新建伏笔上限（防账本灌水）
```

`NovelForgeConfig` 加字段 `settle: SettleConfig = field(default_factory=SettleConfig)`。

`continuity_check_skill.py`：
1. `_SOFT_SYSTEM` 末段（"输出 JSON 数组"起）替换为：

```
输出 JSON 数组，每条：
{"category":"2.3-能力波动","severity":"warn|block","issue":"问题描述",
 "evidence":"逐字引用草稿原文片段","fix":"一句话修改建议","repair_scope":"local|structural"}
repair_scope 判定：OOC（人物根本性走形）/主线偏离/时间线结构性矛盾/视角混乱 → structural；
措辞、局部逻辑、称谓、数值等点状问题 → local。
severity 规则：仅当问题**明确违反上文给出的设定**（禁忌/境界/知情/数值）时用 block，其余用 warn。
evidence 必须逐字摘自草稿原文；没有原文证据的问题不要输出。
若无问题，输出 []。只输出 JSON 数组。
```

2. `_run_soft_check` 解析处：

```python
        text = resp.text.strip()
        if text.startswith("["):
            from ..craft.findings import normalize_findings
            return normalize_findings(json.loads(text), draft_text, "llm_soft")
```

3. `_run_hard_validators` 收尾（return 前）把 issues 过 normalize：

```python
    from ..craft.findings import normalize_findings
    return normalize_findings(issues, ctx.workspace.get("draft_text", ""), "validator")
```

`craft_check_skill.py`：
1. `_issue_dict` 替换为：

```python
def _issue_dict(i: CraftIssue) -> dict:
    return {"check": i.check, "severity": i.severity, "detail": i.detail, "span": i.span,
            # P1#8 findings 字段（旧字段保留，只增不删）
            "category": f"craft.{i.check}", "issue": i.detail, "evidence": i.span,
            "fix": "", "repair_scope": "local", "source": "craft"}
```

2. `_FLAT_SYSTEM` 输出行替换为：

```
输出 JSON 数组，每条：{"category":"flat_character","severity":"warn","issue":"问题描述",
"evidence":"逐字引用草稿原文片段","fix":"一句话修改建议"}
evidence 必须逐字摘自草稿原文，没有证据的问题不要输出。若无问题输出 []。只输出 JSON 数组。
```

3. `_check_flat_character_llm` 解析改为：

```python
        raw = resp.text.strip()
        if raw.startswith("["):
            from ..craft.findings import normalize_findings
            parsed = normalize_findings(json.loads(raw), draft_text, "craft_llm")
            return [CraftIssue(check="flat_character", severity=p["severity"],
                               detail=p["issue"], span=p["evidence"])
                    for p in parsed]
```

- [ ] **Step 4:** `.venv/bin/python -m pytest tests/test_p1_core.py tests/test_m3.py tests/test_m6.py -q` → PASS（既有测试不破）
- [ ] **Step 5:** `git add -A && git commit -m "feat(P1#8): continuity/craft 检查输出 findings 字段——证据强制 + repair_scope"`

---

## Task 4: `_revise` repair_scope 路由 + issues_str 升级

**Files:**
- Modify: `novelforge/control_plane/orchestrator.py`（`_revise`、`_polish`）
- Test: `tests/test_p1_core.py`

- [ ] **Step 1: 写失败测试**：

```python
class TestRepairScopeRouting:
    """structural → 直接全文重写；全 local → 锚点补丁先行（现有回退保留）。"""

    def _factory(self, scope: str, marker: dict):
        body = "陆天踏入山门，他的境界是炼气三层。" + "平铺叙事。" * 150

        def factory(messages, model="", temperature=1.0):
            user = str(messages[-1].content) if messages else ""
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
```

注意：`_factory` 里软检查用 `marker` 只报一次问题——修订后第二轮 check 返回 []，revise 循环正常收敛。

- [ ] **Step 2:** 运行 → FAIL（structural 仍走补丁；issues_str 无 evidence/fix）

- [ ] **Step 3: 实现** —— `orchestrator.py` `_revise` 开头改为：

```python
    def _revise(self, ctx: SkillContext, hard_blocks: list[dict]) -> dict:
        from ..craft.findings import findings_to_issues_str
        draft_text: str = ctx.workspace.get("draft_text", "")
        issues_str = findings_to_issues_str(hard_blocks)
        stable = ctx.workspace.get("stable_context", "")

        # P0#2/P1#8（inkos repair_scope 路由）：结构性问题（OOC/主线偏离/时间线）
        # 补丁救不了，跳过锚点补丁直接全文重写；全 local 才走补丁（失败回退保留）
        structural = any(i.get("repair_scope") == "structural" for i in hard_blocks)
        if not structural and getattr(self._cfg, "patch_revise", True):
```

（原 `if getattr(self._cfg, "patch_revise", True):` 分支整体内容不变，仅条件加 `not structural and`；原 `issues_str = "\n".join(...)` 行删除。）

`_polish` 中 `warns_str = ...` 行替换为：

```python
        from ..craft.findings import findings_to_issues_str
        warns_str = findings_to_issues_str(craft_warns) or "- 整体打磨节奏与钩子"
```

- [ ] **Step 4:** `.venv/bin/python -m pytest tests/test_p1_core.py tests/test_m7.py -q` → PASS（M7 补丁测试不破——其 issue 无 repair_scope，缺省 local 行为不变）
- [ ] **Step 5:** `git add -A && git commit -m "feat(P0#2/P1#8): repair_scope 修订路由——structural 直通重写，issues_str 携带证据与建议"`

---

## Task 5: `craft/foreshadow_settle.py`（结算 + 确定性仲裁）

**Files:**
- Create: `novelforge/craft/foreshadow_settle.py`
- Test: `tests/test_p1_core.py`

- [ ] **Step 1: 写失败测试**：

```python
# ── #6 伏笔结算 ──────────────────────────────────────────────────────────────

def _seed_foreshadow(conn, fs_id="fs_sword", label="断剑之谜",
                     desc="陆天捡到的断剑来历不明", state="planted", due=None):
    conn.execute(
        "INSERT INTO foreshadow(id, label, description, state, planted_chapter, due_chapter)"
        " VALUES(?,?,?,?,1,?)", (fs_id, label, desc, state, due))
    conn.commit()


def _settle_gateway(response: dict):
    """直接构造 gateway 喂 foreshadow_settle（不走整条管线）。"""
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway

    def factory(messages, model="", temperature=1.0):
        return json.dumps(response, ensure_ascii=False)
    fake = FakeProvider(factory=factory)
    return LLMGateway(fake, BudgetLedger(max_tokens=1_000_000, max_usd=10.0)), fake


SETTLE_DRAFT = "陆天握紧断剑，剑身铭文骤亮——这正是十年前山门血案的凶器。他终于明白了断剑的来历。"


class TestForeshadowSettle:
    def test_payoff_with_valid_evidence(self, client, project):
        from novelforge.craft.foreshadow_settle import settle_foreshadow
        conn = _open_conn(project)
        try:
            _seed_foreshadow(conn)
            gw, _ = _settle_gateway({"settlements": [
                {"id": "fs_sword", "action": "payoff",
                 "evidence": "这正是十年前山门血案的凶器"}], "new_hooks": []})
            report = settle_foreshadow(gw, "fast", conn, 5, SETTLE_DRAFT)
            row = conn.execute("SELECT state, paid_off_chapter FROM foreshadow"
                               " WHERE id='fs_sword'").fetchone()
            assert row["state"] == "paid_off" and row["paid_off_chapter"] == 5
            log = conn.execute("SELECT action, evidence FROM foreshadow_log"
                               " WHERE foreshadow_id='fs_sword'").fetchone()
            assert log["action"] == "payoff" and "凶器" in log["evidence"]
            assert report["payoffs"] == 1
        finally:
            conn.close()

    def test_fake_payoff_downgraded_to_mention(self, client, project):
        """evidence 不在终稿 → payoff 降为 mention，state 不变（防假回收核心）。"""
        from novelforge.craft.foreshadow_settle import settle_foreshadow
        conn = _open_conn(project)
        try:
            _seed_foreshadow(conn)
            gw, _ = _settle_gateway({"settlements": [
                {"id": "fs_sword", "action": "payoff", "evidence": "编造的不存在的证据"}],
                "new_hooks": []})
            report = settle_foreshadow(gw, "fast", conn, 5, SETTLE_DRAFT)
            row = conn.execute("SELECT state, last_mentioned_chapter FROM foreshadow"
                               " WHERE id='fs_sword'").fetchone()
            assert row["state"] == "planted"            # 未被假回收
            assert row["last_mentioned_chapter"] == 5   # 降级为 mention
            assert report["payoffs"] == 0 and report["mentions"] == 1
            assert report["dropped_no_evidence"] == 1
        finally:
            conn.close()

    def test_advance_flips_planted_to_reinforced(self, client, project):
        from novelforge.craft.foreshadow_settle import settle_foreshadow
        conn = _open_conn(project)
        try:
            _seed_foreshadow(conn)
            gw, _ = _settle_gateway({"settlements": [
                {"id": "fs_sword", "action": "advance", "evidence": "剑身铭文骤亮"}],
                "new_hooks": []})
            settle_foreshadow(gw, "fast", conn, 5, SETTLE_DRAFT)
            row = conn.execute("SELECT state, advance_count, last_advanced_chapter"
                               " FROM foreshadow WHERE id='fs_sword'").fetchone()
            assert row["state"] == "reinforced"
            assert row["advance_count"] == 1 and row["last_advanced_chapter"] == 5
        finally:
            conn.close()

    def test_unknown_id_dropped(self, client, project):
        from novelforge.craft.foreshadow_settle import settle_foreshadow
        conn = _open_conn(project)
        try:
            _seed_foreshadow(conn)
            gw, _ = _settle_gateway({"settlements": [
                {"id": "fs_nonexistent", "action": "payoff", "evidence": "剑身铭文骤亮"}],
                "new_hooks": []})
            report = settle_foreshadow(gw, "fast", conn, 5, SETTLE_DRAFT)
            assert report["payoffs"] == 0
        finally:
            conn.close()

    def test_new_hook_arbitration_three_branches(self, client, project):
        """高相似→映射 mention；低相似→新建；中间带→拒绝。"""
        from novelforge.craft.foreshadow_settle import settle_foreshadow
        conn = _open_conn(project)
        try:
            _seed_foreshadow(conn)   # 断剑之谜/陆天捡到的断剑来历不明
            gw, _ = _settle_gateway({"settlements": [], "new_hooks": [
                {"label": "断剑之谜", "description": "陆天捡到的断剑来历成谜", "entity": ""},
                {"label": "黑袍人身份", "description": "雪夜出现的黑袍人到底是谁", "entity": ""},
            ]})
            report = settle_foreshadow(gw, "fast", conn, 5, SETTLE_DRAFT)
            # 高相似 → 映射为 fs_sword 的 mention
            row = conn.execute("SELECT last_mentioned_chapter FROM foreshadow"
                               " WHERE id='fs_sword'").fetchone()
            assert row["last_mentioned_chapter"] == 5
            # 低相似 → 新建 planted, origin=settle
            new = conn.execute("SELECT state, origin, planted_chapter FROM foreshadow"
                               " WHERE label='黑袍人身份'").fetchone()
            assert new and new["state"] == "planted" and new["origin"] == "settle"
            assert "黑袍人身份" in report["new_created"]
        finally:
            conn.close()

    def test_new_hooks_capped(self, client, project):
        from novelforge.craft.foreshadow_settle import settle_foreshadow
        conn = _open_conn(project)
        try:
            hooks = [{"label": f"全新伏笔{i}甲乙丙", "description": f"完全不同的新悬念内容{i}",
                      "entity": ""} for i in range(5)]
            gw, _ = _settle_gateway({"settlements": [], "new_hooks": hooks})
            report = settle_foreshadow(gw, "fast", conn, 5, SETTLE_DRAFT, max_new_hooks=2)
            n = conn.execute("SELECT COUNT(*) AS n FROM foreshadow"
                             " WHERE origin='settle'").fetchone()["n"]
            assert n == 2 and len(report["new_created"]) == 2
        finally:
            conn.close()

    def test_bigram_similarity(self):
        from novelforge.craft.foreshadow_settle import _similarity
        assert _similarity("断剑之谜 陆天捡到的断剑来历不明",
                           "断剑之谜 陆天捡到的断剑来历成谜") >= 0.5
        assert _similarity("断剑之谜", "黑袍人身份之谜雪夜") < 0.25
```

- [ ] **Step 2:** 运行 → FAIL（模块不存在）

- [ ] **Step 3: 实现** —— `novelforge/craft/foreshadow_settle.py`：

```python
"""伏笔结算（P1#6，inkos settler/hook-arbiter 同构）。

mention/advance 二分防假回收：
- mention 只记 last_mentioned_chapter，**不改 state**——「被提及 ≠ 被推进」
- advance/payoff 必须有逐字 evidence（空白归一后是终稿子串），否则降为 mention
- 新伏笔不许 LLM 直接建档：先与未解伏笔做字符 bigram Jaccard 确定性仲裁
  （≥0.5 映射为旧伏笔 mention；≤0.25 自动建档 origin='settle'；中间带拒绝）
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Optional

from ..control_plane.llm.tiers import ModelTier
from ..ids import new_id

_OPEN_STATES = ("planted", "reinforced", "misled", "overdue")
_MAP_THRESHOLD = 0.5      # ≥ 此值：映射为既有伏笔的 mention
_NEW_THRESHOLD = 0.25     # ≤ 此值：自动建档；(0.25, 0.5) 拒绝（疑似重述）

_SETTLE_SYSTEM = """\
你是 NovelForge 的伏笔结算员。对照「未解伏笔列表」审读本章终稿，判定每条伏笔在本章的遭遇：
- mention：被提及/暗示，但剧情没有实质推进
- advance：有实质推进（新线索浮现/逼近真相/相关冲突升级）
- payoff：完全兑现回收（真相揭开/承诺兑现/反转落地）
未在本章出现的伏笔不要输出。advance/payoff 必须给 evidence（逐字摘自终稿原文，10-80字）。
另外：若本章埋下了列表之外的新伏笔（明示的未解之谜/预言/反常细节），列入 new_hooks
（label ≤20字、description ≤80字、entity=关联角色名可空）；没有则空数组。
输出 JSON 对象（不要其他说明）：
{"settlements":[{"id":"fs_xxx","action":"mention|advance|payoff","evidence":"原文片段"}],
 "new_hooks":[{"label":"...","description":"...","entity":"..."}]}
"""


def settle_foreshadow(
    gateway, tier: str, conn: sqlite3.Connection, chapter: int, draft_text: str,
    *, max_new_hooks: int = 2,
) -> dict:
    """章末伏笔结算。返回报告 dict（落 detail_json["foreshadow_settle"]）。

    解析/调用失败抛异常由调用方（结算块降级保护）处理。
    """
    report = {"mentions": 0, "advances": 0, "payoffs": 0,
              "new_created": [], "rejected": [], "dropped_no_evidence": 0}
    open_rows = [dict(r) for r in conn.execute(
        "SELECT f.id, f.label, f.description, f.state, f.due_chapter,"
        "       e.canonical_name AS entity_name"
        " FROM foreshadow f LEFT JOIN entities e ON e.id = f.related_entity_id"
        f" WHERE f.state IN ({','.join('?' * len(_OPEN_STATES))})"
        " ORDER BY f.planted_chapter LIMIT 20", _OPEN_STATES).fetchall()]

    data = _call_settler(gateway, tier, open_rows, chapter, draft_text)
    if data is None:
        raise RuntimeError("伏笔结算输出不可解析")

    open_ids = {r["id"] for r in open_rows}
    draft_norm = _norm_ws(draft_text)

    for s in data.get("settlements") or []:
        if not isinstance(s, dict):
            continue
        fs_id = str(s.get("id") or "")
        action = str(s.get("action") or "")
        if fs_id not in open_ids or action not in ("mention", "advance", "payoff"):
            continue
        evidence = str(s.get("evidence") or "").strip()
        # 确定性闸门（防假回收）：advance/payoff 证据必须在终稿中
        if action in ("advance", "payoff") and (
                not evidence or _norm_ws(evidence) not in draft_norm):
            report["dropped_no_evidence"] += 1
            action, evidence = "mention", ""
        _apply_settlement(conn, fs_id, action, chapter, evidence)
        report[action + "s"] = report.get(action + "s", 0) + 1

    created = 0
    for h in data.get("new_hooks") or []:
        if not isinstance(h, dict):
            continue
        label = str(h.get("label") or "").strip()[:40]
        desc = str(h.get("description") or "").strip()[:200]
        if not label:
            continue
        best_id, best_sim = _best_match(label + " " + desc, open_rows)
        if best_sim >= _MAP_THRESHOLD and best_id:
            _apply_settlement(conn, best_id, "mention", chapter, "")
            report["mentions"] += 1
        elif best_sim <= _NEW_THRESHOLD and created < max_new_hooks:
            _create_foreshadow(conn, label, desc, chapter, h.get("entity"))
            report["new_created"].append(label)
            created += 1
        else:
            report["rejected"].append(label)
    conn.commit()
    return report


# ── LLM 调用与解析 ────────────────────────────────────────────────────────────

def _call_settler(gateway, tier: str, open_rows: list[dict],
                  chapter: int, draft_text: str) -> Optional[dict]:
    from ..control_plane.llm.provider import Message
    try:
        mt = ModelTier(tier)
    except ValueError:
        mt = ModelTier.FAST
    fs_lines = "\n".join(
        f"- id={r['id']} label={r['label']} 描述={r['description']}"
        + (f"（第{r['due_chapter']}章到期）" if r["due_chapter"] else "")
        + (f"（角色：{r['entity_name']}）" if r.get("entity_name") else "")
        for r in open_rows) or "（当前没有未解伏笔）"
    # 头 2500 + 尾 3500 字采样：回收/钩子多在章尾。不加 stable 前缀——
    # 结算 system prompt 独有，加了永远不会命中前缀缓存（同 dedup 仲裁的取舍）。
    if len(draft_text) > 6000:
        excerpt = draft_text[:2500] + "\n……（中略）……\n" + draft_text[-3500:]
    else:
        excerpt = draft_text
    resp = gateway.generate(
        mt,
        [Message(role="user", content=(
            f"## 未解伏笔列表\n{fs_lines}\n\n"
            f"## 本章（第 {chapter} 章）终稿（节选）\n{excerpt}"
        ))],
        system=_SETTLE_SYSTEM,
        max_tokens=2048,
    )
    m = re.search(r"\{.*\}", resp.text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# ── 确定性写回 ────────────────────────────────────────────────────────────────

def _apply_settlement(conn, fs_id: str, action: str, chapter: int, evidence: str) -> None:
    if action == "mention":
        conn.execute(
            "UPDATE foreshadow SET last_mentioned_chapter=?, updated_at=datetime('now')"
            " WHERE id=?", (chapter, fs_id))
    elif action == "advance":
        conn.execute(
            "UPDATE foreshadow SET advance_count=advance_count+1,"
            " last_advanced_chapter=?, last_mentioned_chapter=?,"
            " state=CASE WHEN state='planted' THEN 'reinforced' ELSE state END,"
            " updated_at=datetime('now') WHERE id=?", (chapter, chapter, fs_id))
    elif action == "payoff":
        conn.execute(
            "UPDATE foreshadow SET state='paid_off', paid_off_chapter=?,"
            " last_mentioned_chapter=?, updated_at=datetime('now') WHERE id=?",
            (chapter, chapter, fs_id))
    conn.execute(
        "INSERT INTO foreshadow_log(id, foreshadow_id, chapter, action, evidence)"
        " VALUES(?,?,?,?,?)", (new_id("fsl"), fs_id, chapter, action, evidence or None))


def _create_foreshadow(conn, label: str, desc: str, chapter: int, entity) -> None:
    entity_id = None
    if entity:
        row = conn.execute(
            "SELECT id FROM entities WHERE id=? OR canonical_name=? LIMIT 1",
            (str(entity), str(entity))).fetchone()
        entity_id = row["id"] if row else None
    fs_id = new_id("fs")
    conn.execute(
        "INSERT INTO foreshadow(id, label, description, state, planted_chapter,"
        " related_entity_id, importance, origin)"
        " VALUES(?,?,?,'planted',?,?,2,'settle')",
        (fs_id, label, desc, chapter, entity_id))
    conn.execute(
        "INSERT INTO foreshadow_log(id, foreshadow_id, chapter, action, evidence)"
        " VALUES(?,?,?,'plant',NULL)", (new_id("fsl"), fs_id, chapter))


# ── 确定性仲裁（零 LLM）──────────────────────────────────────────────────────

def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _bigrams(s: str) -> set:
    s = _norm_ws(s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else ({s} if s else set())


def _similarity(a: str, b: str) -> float:
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


def _best_match(text: str, open_rows: list[dict]) -> tuple[Optional[str], float]:
    best_id, best_sim = None, 0.0
    for r in open_rows:
        sim = _similarity(text, f"{r['label']} {r['description']}")
        if sim > best_sim:
            best_id, best_sim = r["id"], sim
    return best_id, best_sim
```

- [ ] **Step 4:** `.venv/bin/python -m pytest tests/test_p1_core.py -q` → PASS
- [ ] **Step 5:** `git add -A && git commit -m "feat(P1#6): 伏笔结算——mention/advance 二分 + 证据闸门 + 新伏笔确定性仲裁"`

---

## Task 6: #11 结算降级重构 + SETTLE 接入 orchestrator

**Files:**
- Modify: `novelforge/control_plane/orchestrator.py`（ChapterOutcome + generate_chapter 阶段 6-7 重排 + 新方法 `_settle_chapter`）
- Test: `tests/test_p1_core.py`

- [ ] **Step 1: 写失败测试**：

```python
# ── #11 结算降级 + SETTLE 集成 ───────────────────────────────────────────────

def _plain_factory(messages, model="", temperature=1.0):
    user = str(messages[-1].content) if messages else ""
    if "本章任务" in user:
        return _draft_response("平稳正文。" * 200)
    if "未解伏笔" in user:
        return '{"settlements": [], "new_hooks": []}'
    return "[]"


class TestSettleDegradation:
    def test_gate_failure_degrades_not_kills(self, client, project, monkeypatch):
        """gate 持续抛错 → 重试一次后降级：草稿仍持久化、ok=True、state_degraded。"""
        import novelforge.control_plane.orchestrator as orch_mod
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise RuntimeError("gate 编造故障")
        monkeypatch.setattr(orch_mod.PromotionPolicy, "decide_batch", boom)

        orch, _ = _build_orch(project, _plain_factory)
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn, chapter_goal="测试")
            assert outcome.ok, f"结算失败不应整章报废: {outcome.error}"
            assert outcome.state_degraded
            assert calls["n"] == 2, "canon 结算应重试一次"
            assert outcome.draft_text                      # 正文还在
            row = conn.execute("SELECT COUNT(*) AS n FROM draft_index"
                               " WHERE chapter=1").fetchone()
            assert row["n"] == 1, "草稿必须已落袋"
            run = conn.execute("SELECT status, detail_json FROM pipeline_run"
                               " WHERE run_id=?", (outcome.run_id,)).fetchone()
            assert run["status"] == "completed"
            detail = json.loads(run["detail_json"])
            assert detail["state_degraded"] is True
            assert "gate" in detail["failed_steps"]
            assert detail["unsettled_proposals"], "需留 proposals 快照供修复"
        finally:
            conn.close()

    def test_settle_stage_runs_and_reports(self, client, project):
        """settle 默认开：调用发生且报告落 detail_json。"""
        conn = _open_conn(project)
        try:
            conn.execute(
                "INSERT INTO foreshadow(id, label, description, state, planted_chapter)"
                " VALUES('fs_x','试探','某个未解之谜','planted',1)")
            conn.commit()

            def factory(messages, model="", temperature=1.0):
                user = str(messages[-1].content) if messages else ""
                if "本章任务" in user:
                    return _draft_response("剧情推进，谜团浮现线索。" * 100)
                if "未解伏笔" in user:
                    return json.dumps({"settlements": [
                        {"id": "fs_x", "action": "mention", "evidence": ""}],
                        "new_hooks": []}, ensure_ascii=False)
                return "[]"

            orch, fake = _build_orch(project, factory, settle=True)
            outcome = orch.generate_chapter(2, conn, chapter_goal="测试")
            assert outcome.ok and not outcome.state_degraded
            settle_calls = [c for c in fake.calls
                            if "未解伏笔" in str(c["messages"][-1].content)]
            assert len(settle_calls) == 1
            detail = json.loads(conn.execute(
                "SELECT detail_json FROM pipeline_run WHERE run_id=?",
                (outcome.run_id,)).fetchone()["detail_json"])
            assert detail["foreshadow_settle"]["mentions"] == 1
            row = conn.execute("SELECT last_mentioned_chapter FROM foreshadow"
                               " WHERE id='fs_x'").fetchone()
            assert row["last_mentioned_chapter"] == 2
        finally:
            conn.close()

    def test_settle_disabled_zero_calls(self, client, project):
        orch, fake = _build_orch(project, _plain_factory, settle=False)
        conn = _open_conn(project)
        try:
            orch.generate_chapter(1, conn, chapter_goal="测试")
        finally:
            conn.close()
        assert not any("未解伏笔" in str(c["messages"][-1].content) for c in fake.calls)

    def test_settle_llm_failure_degrades_only_that_step(self, client, project):
        """伏笔结算炸了 → 章仍 ok，degraded 标记，gate 等其余结算不受影响。"""
        def factory(messages, model="", temperature=1.0):
            user = str(messages[-1].content) if messages else ""
            if "本章任务" in user:
                return _draft_response("平稳正文。" * 200)
            if "未解伏笔" in user:
                return "这不是 JSON"
            return "[]"

        orch, _ = _build_orch(project, factory, settle=True)
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn, chapter_goal="测试")
            assert outcome.ok and outcome.state_degraded
            detail = json.loads(conn.execute(
                "SELECT detail_json FROM pipeline_run WHERE run_id=?",
                (outcome.run_id,)).fetchone()["detail_json"])
            assert detail["failed_steps"] == ["foreshadow_settle"]
            assert outcome.fact_ids_committed or outcome.candidates_queued, \
                "canon 结算应已正常完成"
        finally:
            conn.close()
```

注意：`_build_orch`/`_plain_factory` 已在前面任务定义。`test_gate_failure` 里 settle 默认关（fixture 参数 settle=False 缺省），factory 不需伏笔分支也行，但 `_plain_factory` 带了以防默认开。

- [ ] **Step 2:** 运行 → FAIL（ChapterOutcome 无 state_degraded；gate 失败仍整章报废）

- [ ] **Step 3: 实现** —— `orchestrator.py`：

1. `ChapterOutcome` 加字段（quality_dimensions 之后）：

```python
    state_degraded: bool = False   # P1#11: 结算块失败但正文已落袋（连载继续，留修复入口）
```

2. `generate_chapter` 中删除原「── 6. DEDUP + CONFLICT + GATE ──」到「── 7. COMMIT ──」两段（从 `draft_text: str = workspace.get(...)` 到 `_flip_overdue_foreshadow(conn, chapter)`，含 gate progress_cb），替换为：

```python
            # ── 6. COMMIT-DRAFT（P1#11：正文先落袋，结算失败不丢章）─────────
            draft_text: str = workspace.get("draft_text", "")
            proposals: list[dict] = workspace.get("proposals", [])
            draft_id = _persist_draft(conn, draft_text, chapter, cfg.project_id, cfg.db_path)

            # ── 7. SETTLE（canon 结算 + 写回类结算；失败降级不阻塞连载）──────
            settle = self._settle_chapter(skill_ctx, conn, chapter, draft_text,
                                          proposals, pacing, progress_cb)

            run_detail = workspace.get("candidate_report")
            if workspace.get("patch_stats"):
                run_detail = {**(run_detail or {}), "patch_stats": workspace["patch_stats"]}
            if workspace.get("quality_dimensions"):
                run_detail = {**(run_detail or {}),
                              "quality_dimensions": workspace["quality_dimensions"]}
            if settle["foreshadow_settle"] is not None:
                run_detail = {**(run_detail or {}),
                              "foreshadow_settle": settle["foreshadow_settle"]}
            if settle["degraded"]:
                run_detail = {**(run_detail or {}),
                              "state_degraded": True,
                              "settle_error": settle["error"],
                              "failed_steps": settle["failed_steps"],
                              # 修复入口：未结算 proposals 快照，可经 seed API 重放
                              "unsettled_proposals": proposals[:50]}
            _complete_pipeline_run(conn, run_id, draft_id,
                                   detail=run_detail,
                                   quality_score=quality_score,
                                   tokens_spent=ledger.tokens_spent - _tokens0,
                                   usd_spent=ledger.usd_spent - _usd0)

            if progress_cb:
                progress_cb("gate", "degraded" if settle["degraded"] else "ok",
                            {"committed": len(settle["committed_ids"]),
                             "queued": len(settle["queued"]),
                             "state_degraded": settle["degraded"]})
            return ChapterOutcome(
                chapter=chapter,
                ok=True,
                run_id=run_id,
                draft_text=draft_text,
                fact_ids_committed=settle["committed_ids"],
                candidates_queued=settle["queued"],
                issues=all_issues,
                gate=settle["gate_outcome"],
                error=settle["error"] if settle["degraded"] else None,
                usage_tokens=ledger.tokens_spent,
                usage_usd=ledger.usd_spent,
                cache_read_tokens=getattr(ledger, "cache_read_tokens", 0),
                quality_score=quality_score,
                quality_dimensions=workspace.get("quality_dimensions"),
                state_degraded=settle["degraded"],
            )
```

（原 `committed_ids` / `gate_outcome` / `_persist_chapter_summary` / `pacing.update` / `_flip_overdue_foreshadow` 调用全部移入 `_settle_chapter`；原 stage-0 的 `ctx = RunContext(...)` 行也移入。）

3. 新方法 `_settle_chapter`（放 `_quality_pass` 之前）：

```python
    def _settle_chapter(
        self, skill_ctx: SkillContext, conn: sqlite3.Connection, chapter: int,
        draft_text: str, proposals: list[dict], pacing: PacingController,
        progress_cb=None,
    ) -> dict:
        """P1#11（inkos state-degraded）：结算块降级保护——正文已落袋，结算
        任一步失败只标记降级、不丢章。

        步骤 A（canon 结算：candidates→dedup→conflict→gate）失败时清理本次
        创建的候选行后重试一次——重放安全性：候选行已删、dedup 会把与既提交
        事实重复的提案判 merge。步骤 B-E（pacing/摘要/伏笔翻转/伏笔结算）
        各自单次、单独降级，互不阻塞。含 CircuitTripped：正文已花钱生成，
        熔断时丢整章是最差结局，同样走降级。
        """
        cfg = self._cfg
        workspace = skill_ctx.workspace
        out = {"degraded": False, "error": None, "failed_steps": [],
               "gate_outcome": None, "committed_ids": [], "queued": [],
               "foreshadow_settle": None}

        # ── 步骤 A：canon 结算（失败清理后重试一次）──────────────────────────
        for attempt in (1, 2):
            created: list[str] = []
            try:
                ctx = RunContext(conn=conn, policy_mode=cfg.governance.mode,
                                 actor="orchestrator")
                candidates = _proposals_to_candidates(proposals, chapter, conn)
                created = [c.candidate_id for c in candidates]
                dedup_gw = self._gw if cfg.dedup.enable_llm_arbiter else None
                dedup_engine = DeduplicationEngine(
                    bm25_gap_min=cfg.dedup.bm25_gap_min, llm_gateway=dedup_gw)
                candidates = _apply_dedup(candidates, dedup_engine, conn)

                conflict_map: dict[str, ConflictSet] = {}
                for cand in candidates:
                    cset = detect_conflict(cand, conn)
                    cand.risk_tier = classify_risk(cand, cfg)
                    if cset.has_block:
                        conflict_map[cand.candidate_id] = cset
                        try:
                            conn.execute(
                                "UPDATE fact_candidates SET risk_tier=? WHERE candidate_id=?",
                                (cand.risk_tier, cand.candidate_id))
                        except Exception:
                            pass
                conn.commit()

                world = workspace.get("world_state")
                gate_decision: GateDecision = PromotionPolicy.decide_batch(
                    candidates, world, cfg, conflict_map=conflict_map)
                gate_outcome: GateOutcome = apply_gate_routes(
                    ctx, gate_decision, {"chapter": chapter})
                out["gate_outcome"] = gate_outcome
                out["committed_ids"] = [fid for _, fid in gate_outcome.committed]
                out["queued"] = gate_outcome.queued
                break
            except Exception as e:
                try:
                    if created:
                        ph = ",".join("?" * len(created))
                        conn.execute(
                            f"DELETE FROM fact_candidates WHERE candidate_id IN ({ph})",
                            created)
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                if attempt == 2:
                    out["degraded"] = True
                    out["failed_steps"].append("gate")
                    out["error"] = f"gate: {e}"

        # ── 步骤 B-E：写回类结算，各自降级 ───────────────────────────────────
        def _step(name: str, fn) -> None:
            try:
                fn()
            except Exception as e:
                out["degraded"] = True
                out["failed_steps"].append(name)
                if out["error"] is None:
                    out["error"] = f"{name}: {e}"

        beats = workspace.get("beats", [])
        _step("pacing", lambda: pacing.update(chapter, beats, len(draft_text), conn))
        if getattr(cfg.recall, "enable_summaries", True) and draft_text:
            _step("summary", lambda: _persist_chapter_summary(
                conn, self._gw, chapter, draft_text))
        _step("foreshadow_flip", lambda: _flip_overdue_foreshadow(conn, chapter))

        scfg = getattr(cfg, "settle", None)
        if scfg is not None and scfg.enabled and draft_text:
            def _do_settle() -> None:
                from ..craft.foreshadow_settle import settle_foreshadow
                out["foreshadow_settle"] = settle_foreshadow(
                    self._gw, scfg.tier, conn, chapter, draft_text,
                    max_new_hooks=scfg.max_new_hooks)
            _step("foreshadow_settle", _do_settle)
            if progress_cb:
                progress_cb("settle",
                            "degraded" if "foreshadow_settle" in out["failed_steps"]
                            else "ok",
                            out["foreshadow_settle"] or {})
        return out
```

注意：generate_chapter 顶部原有的 `ctx = RunContext(...)`（stage 0）删除——已移入 `_settle_chapter`。检查文件内 `ctx` 其他引用（应没有：`skill_ctx` 才是贯穿变量）。

- [ ] **Step 4:** `.venv/bin/python -m pytest tests/test_p1_core.py -q` → PASS
- [ ] **Step 5:** `.venv/bin/python -m pytest tests/ -q` → 全量 PASS（重排不破既有管线测试）
- [ ] **Step 6:** `git add -A && git commit -m "feat(P1#11/#6): 结算降级不阻塞——正文先落袋 + canon 结算重试 + SETTLE 阶段接入"`

---

## Task 7: API / autopilot / chapter_suggest 接线

**Files:**
- Modify: `novelforge/app/api/orchestrator.py`（两处 final_gate + `_load_run_detail`）
- Modify: `novelforge/app/models.py`（PipelineRunDetail + ForeshadowResponse）
- Modify: `novelforge/app/autopilot_manager.py`（state_degraded 计入降级）
- Modify: `novelforge/app/api/craft.py`（`_row_to_foreshadow` 新列）
- Modify: `novelforge/app/chapter_suggest.py`（伏笔条目带角色名）
- Test: `tests/test_p1_core.py`

- [ ] **Step 1: 写失败测试**：

```python
# ── API / autopilot 接线 ─────────────────────────────────────────────────────

class TestApiWiring:
    def test_run_detail_exposes_degraded_and_settle(self, client, project, monkeypatch):
        import novelforge.control_plane.orchestrator as orch_mod

        def boom(*a, **k):
            raise RuntimeError("gate 编造故障")
        monkeypatch.setattr(orch_mod.PromotionPolicy, "decide_batch", boom)
        orch, _ = _build_orch(project, _plain_factory)
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn, chapter_goal="测试")
        finally:
            conn.close()
        detail = client.get(f"/v1/projects/{project}/pipeline/runs/{outcome.run_id}").json()
        assert detail["state_degraded"] is True

    def test_foreshadow_api_exposes_settle_columns(self, client, project):
        client.post(f"/v1/projects/{project}/foreshadow",
                    json={"label": "测试伏笔", "description": "描述",
                          "planted_chapter": 1})
        rows = client.get(f"/v1/projects/{project}/foreshadow").json()
        assert rows and "advance_count" in rows[0] and "origin" in rows[0]

    def test_autopilot_counts_state_degraded(self):
        """state_degraded 计入 consecutive_hard_issues 的判定逻辑（单元级）。"""
        from novelforge.control_plane.orchestrator import ChapterOutcome
        outcome = ChapterOutcome(chapter=1, ok=True, state_degraded=True)
        hard_issues = [i for i in (outcome.issues or []) if i.get("severity") == "block"]
        assert not hard_issues
        assert getattr(outcome, "state_degraded", False)   # autopilot 判定条件可触发
```

（autopilot 集成行为由现有 test_m8 自动挂机测试覆盖路径；此处只锁判定输入。POST foreshadow 的请求体字段以 `app/api/craft.py:144` 的实际 Request model 为准，执行时核对。）

- [ ] **Step 2:** 运行 → FAIL

- [ ] **Step 3: 实现**

`app/models.py`：
- `PipelineRunDetail` 加：

```python
    state_degraded: bool = False         # P1#11: 结算降级（正文已落袋）
    foreshadow_settle: Optional[dict] = None   # P1#6: 伏笔结算报告
```

- `ForeshadowResponse` 加：

```python
    last_mentioned_chapter: Optional[int] = None
    advance_count: int = 0
    last_advanced_chapter: Optional[int] = None
    origin: str = "manual"
```

`app/api/orchestrator.py`：
- 两处 final_gate（非流式 :97 与 SSE :172）改为：

```python
        final_gate = (
            "state_degraded" if getattr(outcome, "state_degraded", False)
            else "committed_canon" if outcome.fact_ids_committed
            else "enqueued_review" if outcome.candidates_queued
            else "no_candidates"
        )
```

- `_load_run_detail` 的 detail_json 解析块加：

```python
            state_degraded = bool(detail.get("state_degraded"))
            foreshadow_settle = detail.get("foreshadow_settle")
```

（在 try 前初始化 `state_degraded = False`、`foreshadow_settle = None`），并把两个值传入返回的 `PipelineRunDetail(...)`。

`app/autopilot_manager.py` :444 判定行改为：

```python
                    if hard_issues or low_quality or getattr(outcome, "state_degraded", False):
```

（注释补一句：`# P1#11：结算降级计入连续问题——连续失败自然转人审`）

`app/api/craft.py` `_row_to_foreshadow` 加四个字段透传：

```python
        last_mentioned_chapter=row["last_mentioned_chapter"],
        advance_count=row["advance_count"],
        last_advanced_chapter=row["last_advanced_chapter"],
        origin=row["origin"],
```

`app/chapter_suggest.py` 伏笔查询（:67）改为 JOIN 带角色名：

```python
    fs_rows = conn.execute(
        "SELECT f.label, f.due_chapter, e.canonical_name AS entity_name"
        " FROM foreshadow f LEFT JOIN entities e ON e.id = f.related_entity_id"
        " WHERE f.state IN ('planted','reinforced','misled','overdue')"
        "   AND f.due_chapter IS NOT NULL AND f.due_chapter<=?"
        " ORDER BY f.due_chapter LIMIT 5",
        (chapter + 2,),
    ).fetchall()
```

两处 label 拼装改为（overdue 与 upcoming 同样处理）：

```python
            labels = "、".join(
                f"{r['label']}（{('角色：' + r['entity_name'] + '，') if r['entity_name'] else ''}"
                f"第{r['due_chapter']}章{'已' if r['due_chapter'] < chapter else ''}到期）"
                for r in overdue   # / upcoming
            )
```

- [ ] **Step 4:** `.venv/bin/python -m pytest tests/ -q` → 全量 PASS
- [ ] **Step 5:** `git add -A && git commit -m "feat(P1): API/autopilot 接线——state_degraded 透出与降级计数、伏笔新列、到期伏笔挂角色名"`

---

## Task 8: 前端最小集

**Files:**
- Modify: `web/src/api/types.ts`
- Modify: `web/src/components/studio/Studio.tsx`

- [ ] **Step 1:** `types.ts` 的 `PipelineRunDetail` 加：

```typescript
  state_degraded?: boolean;            // P1#11: 结算降级（正文已落袋，世界状态待修复）
  foreshadow_settle?: {
    mentions?: number; advances?: number; payoffs?: number;
    new_created?: string[]; rejected?: string[];
  } | null;
```

- [ ] **Step 2:** `Studio.tsx`：
  1. 历史行（quality/$ chip 同排处）加降级徽章：

```tsx
{runDetails[r.run_id]?.state_degraded && (
  <span className="chip chip-warn" title="结算降级：正文已保存，世界状态写回失败，可重放提案修复">⚠ 结算降级</span>
)}
```

（chip 类名/结构照同文件既有 $ chip 的写法，保持一致；若历史行处拿不到 detail，放到展开详情区顶部。）
  2. 展开详情区（DimensionChips/PatchStatsChips 同区）加结算摘要行：

```tsx
{d.foreshadow_settle && (
  <div className="muted" style={{ fontSize: 12 }}>
    伏笔结算：回收 {d.foreshadow_settle.payoffs ?? 0} · 推进 {d.foreshadow_settle.advances ?? 0}
    · 提及 {d.foreshadow_settle.mentions ?? 0} · 新建 {(d.foreshadow_settle.new_created ?? []).length}
  </div>
)}
```

（变量名 `d`/`runDetails` 以文件实际为准；插入点在 PatchStatsChips 渲染之后。样式跟随邻近元素。）

- [ ] **Step 3:** `cd web && npm run build` → PASS（tsc -b + vite）
- [ ] **Step 4:** `git add -A && git commit -m "feat(P1): 前端最小集——结算降级徽章 + 伏笔结算摘要"`

---

## Task 9: 收尾

- [ ] **Step 1:** `.venv/bin/python -m pytest tests/ -q` 全量 + `cd web && npm run build` → 双绿
- [ ] **Step 2:** 用 superpowers:finishing-a-development-branch 处理分支（用户偏好：本地合入 main，合并后在 main 重跑全量测试，删分支，不 push）

---

## Self-Review 记录

- Spec 覆盖：#8（Task 2/3/4）、#6（Task 1/5/6/7）、#11（Task 6/7）、前端最小集（Task 8）、迁移双路径（Task 1）✓
- spec §三「重试一次」细化为「步骤 A 清理候选行后重试一次 + B-E 单独降级」，理由见执行注意（候选行 new_id 不幂等，整块盲重放会留重复行）。
- 类型一致：`settle_foreshadow(gateway, tier, conn, chapter, draft_text, *, max_new_hooks)`、`_settle_chapter(skill_ctx, conn, chapter, draft_text, proposals, pacing, progress_cb)`、report 键 `mentions/advances/payoffs/new_created/rejected/dropped_no_evidence`、detail_json 键 `state_degraded/settle_error/failed_steps/unsettled_proposals/foreshadow_settle` 全文一致 ✓
- 已知执行期需核对点（已在任务内标注）：Task 1 新库建库入口名；Task 7 POST foreshadow 请求体字段；Task 8 Studio.tsx 具体变量名/插入点。
