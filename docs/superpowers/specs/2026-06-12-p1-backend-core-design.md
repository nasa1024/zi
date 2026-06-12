# P1 后端核心三项设计：findings 化审稿 / 伏笔结算 / 结算降级

日期：2026-06-12　来源：docs/research_inkos_oh-story_20260612.md 的 P1 #6/#8/#11（含 P0#2 repair_scope 路由，与 #8 同一处 schema 改造）
范围：仅后端 + 前端最小集。#7 章节卡契约、#9 风格锚、#10 爽点看板留下一批。
用户已确认：伏笔结算默认开（FAST 档）；新伏笔纯确定性仲裁（不走人审队列）。

## 一、#8 审稿 findings 化 + repair_scope 路由

### Finding 统一结构（新模块 `novelforge/craft/findings.py`）

```python
{
  "severity": "block" | "warn",        # 保留现有两级；不引入 S1-S4
  "category": "continuity.2.3-能力波动" | "craft.hook" | ...,
  "evidence": "草稿原文片段",           # LLM 来源必填且必须是草稿子串
  "issue":    "问题描述",
  "fix":      "一句话修改建议（可空）",
  "repair_scope": "local" | "structural",   # 缺省 local
  "source":   "validator" | "llm_soft" | "craft",
}
```

模块函数：
- `normalize_findings(raw: list, draft_text: str, source: str) -> list[dict]`
  —— 逐字段宽容解析（畸形字段丢该条不崩整轮）；旧字段兼容映射
  （`desc`→`issue`、`span`→`evidence`、`subclass`/`check`→`category`、
  `detail`→`issue`）；**evidence 校验**：source 为 LLM（`llm_soft`/`craft` 的
  LLM 子检查）且 evidence 经空白归一后不是 draft 子串 → 丢弃整条
  （oh-story「无证据不输出」）。validator 来源不强制 evidence。
  severity 非法值→`warn`；repair_scope 非法值→`local`。
- `findings_to_issues_str(findings: list[dict]) -> str`
  —— 给补丁/重写 prompt 的三行体：`- [category] issue / 原文：「evidence」 / 建议：fix`。
  evidence 是锚点补丁 find 字段的天然候选，预期提升锚定成功率（看板已有指标可验证）。

### 接入点

- `skills/continuity_check_skill.py` `_SOFT_SYSTEM`：输出字段改为
  `{"category","severity","issue","evidence","fix","repair_scope"}`；
  repair_scope 判定规则写进 prompt（OOC/主线偏离/时间线矛盾/视角混乱 →
  structural；措辞/局部逻辑/称谓数值 → local）。解析后过 `normalize_findings`。
- `skills/craft_check_skill.py`：确定性 CraftIssue 转 finding 形
  （source="craft"，evidence=span 或空，repair_scope=local）；
  `_check_flat_character_llm` 的 prompt 增加 evidence/fix 字段并过 normalize。
- `control_plane/orchestrator.py`
  - `_revise(ctx, hard_blocks)`：**路由**——`any(repair_scope=="structural")`
    → 跳过补丁直接全文重写；全 local → 锚点补丁优先（现有失败回退保留）。
    issues_str 换 `findings_to_issues_str`。
  - `_polish` 的 warns_str 同样换新格式。
- workspace 键名不变（`continuity_issues` / `craft_issues`），元素字段变富，
  现有消费方（severity 过滤、autopilot hard_issues 统计）零改动。

## 二、#6 伏笔账本精细化（mention/advance 二分 + 确定性仲裁）

### 迁移 v12（`db/migrations/v12.py` + schema.sql 基线同步 + SCHEMA_VERSION="12"）

```sql
ALTER TABLE foreshadow ADD COLUMN last_mentioned_chapter INTEGER;
ALTER TABLE foreshadow ADD COLUMN advance_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE foreshadow ADD COLUMN last_advanced_chapter INTEGER;
ALTER TABLE foreshadow ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual';  -- manual|settle
CREATE TABLE foreshadow_log (
    id            TEXT PRIMARY KEY,
    foreshadow_id TEXT NOT NULL REFERENCES foreshadow(id),
    chapter       INTEGER NOT NULL,
    action        TEXT NOT NULL CHECK(action IN ('plant','mention','advance','payoff')),
    evidence      TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_fslog_fs ON foreshadow_log(foreshadow_id, chapter);
```

### 新模块 `novelforge/craft/foreshadow_settle.py`

- `settle_foreshadow(gateway, tier, conn, chapter, draft_text, *, max_new_hooks=2) -> dict`
  1. 取未解伏笔（state IN planted/reinforced/misled/overdue，按 planted_chapter
     正序，≤20 条；带 related_entity 名）。未解伏笔为空时**仍调用**——
     新伏笔发现依赖这次调用；settle.enabled=False 时整段跳过、零调用。
  2. 一次 FAST 调用。draft 采样：头 2500 + 尾 3500 字（回收多在章尾）。
     **不加 stable 前缀**（system prompt 独有，永不命中前缀缓存——同 non-goal#1 逻辑）。
     输出：`{"settlements":[{"id","action":"mention|advance|payoff","evidence"}],
     "new_hooks":[{"label","description","entity"}]}`；逐字段宽容解析。
  3. **确定性闸门（防假回收）**：advance/payoff 的 evidence 经空白归一后必须是
     draft_text 子串，否则**降为 mention**；id 不在未解列表的判定丢弃。
  4. 应用：
     - mention → `last_mentioned_chapter=chapter`，log(mention)。**state 不变**。
     - advance → planted→reinforced（其余 state 不变），advance_count+=1，
       last_advanced_chapter，log(advance, evidence)。
     - payoff → state='paid_off'，paid_off_chapter=chapter，log(payoff, evidence)。
  5. **新伏笔仲裁 `arbitrate_new_hooks`（零 LLM）**：候选 (label+description) 与
     每条未解伏笔 (label+description) 做**字符 bigram Jaccard**：
     ≥0.5 → 映射为该伏笔的 mention；≤0.25 → 自动建档（planted、当前章、
     importance=2、origin='settle'、related_entity 走 `_resolve_entity`，log(plant)）；
     (0.25,0.5) → 拒绝（疑似重述，记入报告不落库）。每章新建 ≤ max_new_hooks。
  6. 返回报告 `{"mentions":n,"advances":n,"payoffs":n,"new_created":[labels],
     "rejected":[labels],"dropped_no_evidence":n}` → 并入
    `detail_json["foreshadow_settle"]`。
- 配置：`config.py` 新 `SettleConfig(enabled: bool = True, tier: str = "fast",
  max_new_hooks: int = 2)`，挂 `NovelForgeConfig.settle`。
- orchestrator：COMMIT 后新 SETTLE 子步（位于 #11 结算保护块内），
  progress_cb("settle", "ok"|"degraded", 报告)。
- 消费侧小改：`app/chapter_suggest.py` 的伏笔条目带角色名
  （JOIN entities，`label（角色：X，第N章到期）`）。
- `app/api/craft.py` ForeshadowResponse 增加新列字段（只读透传）。

## 三、#11 结算降级不阻塞

`generate_chapter` 阶段重排（现状：6 dedup/gate → 7 persist draft/摘要）：

1. 质量评分后立即 `_persist_draft`（正文先落袋）。
2. 其后划为**结算块**：proposals→candidates、dedup、conflict、gate、
   摘要、pacing.update、`_flip_overdue_foreshadow`、foreshadow settle。
   `_complete_pipeline_run` 在结算块成败两条路径上都最后执行
   （成功：正常 detail；失败：带 state_degraded 标记的 detail）。
3. 结算块整体 try：失败（含 CircuitTripped——正文已花钱，丢整章是最差结局）
   → **同层重试一次**（重新执行结算块，不动正文；幂等性依据：
   fact_candidates INSERT OR IGNORE + UPDATE 类写回可重放）→ 仍败：
   - `detail_json` 增 `state_degraded: true`、`settle_error: str`、
     `unsettled_proposals`（前 50 条快照，修复入口=现有 seed API 重放，不新增端点）；
   - run 照常 completed（含 quality/cost）；
   - outcome：`ok=True`、新字段 `state_degraded: bool = False`、error=结算错误；
   - API `final_gate="state_degraded"`（SSE done 事件同步）。
4. autopilot（`app/autopilot_manager.py`）：`outcome.state_degraded` 计入
   `consecutive_hard_issues`（连续结算失败自然触发现有转人审降级，零新配置）。
5. 全局 except 仍兜底草稿生成阶段的失败（行为不变：ok=False）。

## 四、前端最小集

- `web/src/api/types.ts`：`PipelineRunDetail` 加 `state_degraded?: boolean`、
  `foreshadow_settle?: Record<string, unknown>`；`SSEDoneEvent.final_gate` 注释补值。
- `Studio.tsx`：历史行 state_degraded 徽章（⚠ 结算降级）；展开详情显示
  foreshadow_settle 一行摘要（回收 n/推进 n/新建 n）。
- 后端 `models.py` PipelineRunDetail + `_load_run_detail` 透传两字段。

## 五、测试（.venv/bin/python -m pytest，FakeProvider 风格）

新文件 `tests/test_p1_core.py`：
1. findings：evidence 不在稿中→丢弃；旧字段名兼容；畸形字段丢条不崩；
   structural 存在→_revise 不调补丁直接重写（FakeProvider 断言调用序列）；
   全 local→补丁先行；issues_str 含 evidence/fix。
2. settle：payoff+有效 evidence→paid_off+log；payoff+伪 evidence→降 mention、
   state 不变；advance 计数与 reinforced 翻转；仲裁三分支（bigram Jaccard
   构造三档相似度样例）；每章新建上限；enabled=False 零 LLM 调用
   （FakeProvider calls 计数）；未知 id 丢弃。
3. 降级：monkeypatch gate 抛错→重试 1 次→outcome.ok=True+state_degraded、
   草稿已持久化（draft_index 有行）、detail_json 带 unsettled_proposals；
   autopilot 计数（构造 outcome 喂 _run_loop 的判定段或集成跑）。
4. 迁移：v12 双路径（新库 schema.sql / 旧库 migrations 链）列与表断言。
5. 既有 368 测试全绿（craft_check/continuity 输出字段变富但键名兼容）。

## 六、明确不做（本批）

- S1-S4 四级严重度（block/warn 贯穿全管线，改造纯增熵）。
- 伏笔候选复用 fact_candidates / review_queue（伏笔是叙事承诺非世界事实，
  硬塞 facts 晋升流污染 gate 语义；已选确定性仲裁）。
- 新修复端点（unsettled_proposals 快照 + 现有 seed API 已构成修复通路）。
- #7/#9/#10 与三条既定 non-goal（见 memory）不在本批。
