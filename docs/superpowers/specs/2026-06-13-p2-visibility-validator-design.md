# P2#12 原型设计：per-character visibility 单项 validator + 硬校验接线修复

日期：2026-06-13
来源：`docs/research_inkos_oh-story_20260612.md` P2 #12（inkos §3.1.5 时态边 + per-character visibility）
授权：用户明示"不用和我确认，选择最合适的方式进行"。

## 0. 勘察发现（决定本批范围的关键事实）

`continuity_check_skill._run_hard_validators` 的硬校验路径是死代码：

1. `from ..validators.items import validate_item_conservation` —— 该名不存在（真名 `validate_item_inventory`），ImportError 被外层 try 静默吞掉，**三个确定性 validator 在管线里从未跑过**；
2. `extract_claims_rule(draft_text)` 缺必填 `chapter` 参数（还应传 conn 以加载境界词表）；
3. validator 输出 `Issue.severity ∈ {critical,major,minor,info}`，而 findings 体系只认 `{block,warn}`——`normalize_findings` 把不认识的 severity 一律归 warn，即使接通了，critical 也永远不会 block；
4. 同款坏调用还在 `governance/conflict.py:108`（缺 chapter）和 `craft/candidate_judge.py:112`（缺 chapter，且 `_count_hard_blocks` 数 severity=="block" 而 Issue 从不产出 "block"——多候选硬校验否决层同样失效）。

`validate_knowledge_edges`（知情者越权，KNOWLEDGE_LEAK）已实现且有单测，但从未接进管线——P2#12 要解决的"角色知道了不在场的事"，一半能力已经躺在仓库里。

## 1. 范围

**做**：
- A. 修通 `_run_hard_validators`：正确 import/签名、Issue→finding 显式转换（severity 映射 critical→block，major/minor/info→warn；code→category；message→issue；suggested_fix→fix）、接入 knowledge + 新 presence validator；
- B. 新 validator `validators/presence.py::validate_event_visibility`（P2#12 原型本体）；
- C. 修复 `conflict.py` / `candidate_judge.py` 的坏调用（chapter/conn 补齐；`_count_hard_blocks` 数 critical）。

**不做**（原型阶段明确不碰）：
- 不改 schema（不加时态边表、不加 visibility 列）——原型用现有 `timeline_events.participants` + `knowledge_edges`；
- 不做世界投影改造（per-character world projection），评估原型误报率后再排期；
- 不做 LLM 辅助匹配——纯确定性。

## 2. validate_event_visibility 设计

**问题**："X 在第 N 章得知了某事，但事发时 X 不在场，也没人告诉他。"

**输入**：claims（`ClaimType.KNOWLEDGE`，由 `_KNOWLEDGE_PATTERN` 从"X知道/发现/得知/察觉/看穿/识破了Y"抽取，payload.info_key=Y）+ world + conn。

**判定流程**（每条 KNOWLEDGE claim）：
1. exempt_tags 含 planted_misdirection/unreliable_narrator → 跳过（与 knowledge validator 同免）；
2. subject 实体解析（canonical_name → alias）；解析失败 → 跳过（抽取噪声不报）；
3. X 已有 info_key 的 knowledge_edge（learned_chapter ≤ N）→ 合法（有人转告/亲历已记账）；
4. info 已公开（public_from_chapter ≤ N）→ 合法；
5. 在 `timeline_events`（chapter ≤ N）中找**标题与 info_key 匹配**的事件：互相包含 或 字符 bigram overlap ≥ 0.6；
6. 无匹配事件 → 沉默（没有在场证据可查，归 KNOWLEDGE_LEAK 兜底，本 validator 不重复报）；
7. 有匹配事件且**任一**事件的 participants 含 X（按 entity_id 或 canonical_name 比对）→ 合法（亲历）；
8. 否则 → `KNOWLEDGE_NO_PRESENCE`，severity=**major**（原型期 warn 级，观察误报率后再升级），message 带事发章/地点/在场名单，suggested_fix 提示补 knowledge_edge 或改写。

**与现有 KNOWLEDGE_LEAK 的关系**：LEAK 查"知情集里没有"（critical/block）；NO_PRESENCE 在 LEAK 基础上多给一层**事发现场证据**（在场名单具体到人），且仅在能找到对应事件时触发。两者同时报时信息互补不冲突。

## 3. 接线（_run_hard_validators 重写）

```
claims = extract_claims_rule(draft_text, ctx.target_chapter, ctx.conn)
for validate in (validate_power_monotonicity, validate_item_inventory,
                 validate_knowledge_edges, validate_event_visibility):
    issues += validate(claims, world, ctx.conn)   # 各自 try/except 隔离
findings = [_issue_to_finding(i) for i in issues]
```

`_issue_to_finding`：`{"severity": critical→block else warn, "category": code,
"issue": message, "evidence": span(claim 原文，validator 来源不强制),
"fix": suggested_fix or "", "repair_scope": "local", "source": "validator"}`。
Issue 没带 span——claims 有 span；按 claim_id 回查带上（evidence 给锚点补丁用）。

world 为 None 时整段跳过（与现状一致）。

## 4. 连带修复

- `conflict.py::_validator_conflicts`：`extract_claims_rule(draft_text, prop.get("valid_from_chapter", 0), conn)`；
- `candidate_judge._count_hard_blocks`：补 chapter（用 `world.as_of + 1`，world None 时 0）与 conn（`world._conn`）；block 计数改为数 `severity == "critical"`（Issue 域）；
- 这两处行为变化：硬校验从"永远 0 问题"变成真跑——多候选否决层、提案冲突检测随之激活。

## 5. 测试

`tests/validators/test_presence.py`：在场→沉默；不在场且无边不公开→NO_PRESENCE；有边→沉默；公开→沉默；无匹配事件→沉默；实体解析失败→沉默。
`tests/test_p2_wiring.py`：管线级——seed canon 后 `_run_hard_validators` 产出 block 级 KNOWLEDGE_LEAK finding（证明接线通了）；presence warn finding；power 境界倒退 block；`_count_hard_blocks` 对倒退候选计数 > 0。
全量回归确认激活硬校验后既有测试不雪崩（fixture 里的草稿可能触发新告警——逐个核对是真问题还是 fixture 需调整）。

## 6. 风险

激活沉睡两个月的校验层可能让现有 E2E fixture 的草稿文本撞上 validator（如数字+单位被 numeric 抽取）。本批只接 power/item/knowledge/presence 四个（numeric 不接，它的 claims 噪声最大），且 major 以下归 warn 不阻断，风险可控。
