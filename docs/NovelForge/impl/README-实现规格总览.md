# NovelForge 实现级规格 · 总览与阅读顺序

> 本目录（`docs/NovelForge/impl/`）是设计稿 **§12（Agent 循环与工具调用）** 与 **§14（LLM 接入层）** 的**实现级落地**——从"伪代码"推进到"近真代码"：完整 Python 类型注解、函数签名、近真函数体、异常层级、边界清单、Mermaid 时序图、文件布局树与 pytest 测试计划。三篇规格与已落地的 Python 包 **`novelforge/` 同构**：规格里出现的每个模块路径都对应 `novelforge/{llm,tools,skills,db}/*.py`，可直接照着写。设计稿 §07/§12/§14 中的 `control_plane/*`、`governance/*`、`skills/*` 等路径均为**示意**，本实现规格一律改用 `novelforge/` 包路径（详见 `00-接缝契约与文件布局.md` §5/附录命名对齐表）。

---

## 文件清单与阅读顺序

**严格按此顺序读**（接缝是另两篇的单一权威边界，必须先读）：

1. **`00-接缝契约与文件布局.md`** —— *先读*。§12↔§14 共享的"归一化类型 / 公开接口签名 / 调用契约 / 文件布局 / 依赖方向 / 测试分层"的**单一权威定义**。接缝四件套（`novelforge/llm/types.py`、`novelforge/llm/errors.py`、`novelforge/llm/tiers.py`、`novelforge/tools/errors.py`）在此定义一次，另两篇一律 `import` 引用、绝不重定义。包含：
   - §1 共享归一化类型（`ChatMessage`/`Tool`/`ToolCall`/`ToolResult`/`Usage`/`Pricing`/`CacheHint`/`Response`/`ProviderStreamEvent`/`CapabilitySet`/`ModelTier`/`StopReason`/`StreamEventType` + `LLMUsage` 别名）
   - §1.11 双根异常层级（`LLMError` 系 / `ToolError` 系，互不继承）
   - §2 `LLMProvider` 协议 + `LLMGateway` 公开接口签名
   - §3 `ToolRegistry` / `ToolContext` / `ToolLoop` 公开签名
   - §4 **ToolLoop ↔ LLMGateway 调用契约**（主接缝）+ 终止条件 + 错误处置分工 + 时序图
   - §5 完整文件布局树、§6 依赖方向图（禁止环）、§7 三层测试策略

2. **`14-LLM接入层-实现规格.md`** —— *次读*。§14 接入层四层（Skill→Gateway→Provider→各厂商）落到 `novelforge/llm/*`。包含：config 加载（key 仅 env）、pricing 归一化、prompt 缓存纪律、提示式工具兜底、结构化输出修复重试、流式装配、Gateway（档→model / 降级 / 退避 / 回退 / 记账）、三家 Provider（Anthropic/OpenAICompat/Local）请求装配与响应解析（精确到字段）、`FakeProvider` 测试核心、边界清单、时序图、pytest 计划。

3. **`12-Agent工具循环-实现规格.md`** —— *末读*。§12 受限 ReAct 工具循环落到 `novelforge/tools/*`。包含：`ToolRegistry`（双护栏）、6 个 MVP 只读工具的**确定性 SQL 全文**（逐列对照 §02 DDL）、`ToolLoop`（有界/去重/截断/终止/优雅降级）、`tool_call_log` 审计 DDL、缓存纪律、边界清单、时序图、`ChapterDraftSkill` 端到端、pytest 计划。

> 为什么这个顺序：`tools/*` 依赖 `llm/*`（调 Gateway、用归一化类型），而两者都依赖接缝四件套；接缝零上游依赖。读完接缝就掌握了所有共享类型与调用契约，再读 §14（被调方）、最后读 §12（调用方）时，所有跨节符号都已有定义。

---

## 依赖方向（单向，禁止环）

```
skills/*  ──>  tools/*  ──>  llm/*  ──>  接缝四件套（汇点，零上游依赖）
   │              │            ▲
   └──────────────┴────────────┘   （skills 与 tools 也直接依赖接缝类型）
db/* + contracts.py  <──  tools/builtin/*、tools/audit.py、llm/structured.py
```

硬规则（接缝篇 §6）：

- `llm/*` **绝不** `import` `novelforge.tools` / `novelforge.skills`（Gateway/Provider 不知道工具循环与 Skill 存在，只见接缝类型）。
- `tools/*` 可 `import novelforge.llm.*`，但**绝不** `import novelforge.skills.*`；对 `SkillContract`/`SkillContext`/`CircuitBreaker`/`RepositoryBundle`/`RestrictedWorkspace` 仅用**字符串前向引用注解**（`from __future__ import annotations`），运行时由 `skills/*` 注入。
- `skills/*` 可 `import` `tools/*` 与 `llm/*`（顶层编排）。
- 接缝四件套是 DAG 汇点，可被任意层 import。
- 由 `tests/test_seam_contract.py::test_no_import_cycles`（ast 静态扫描）守护，防漂移。

---

## 从规格到代码的落地步骤

按依赖拓扑自底向上落地，每步可独立单测（`FakeProvider` + in-memory sqlite，零外网零 SDK）：

1. **接缝四件套（最先）**
   - `novelforge/llm/types.py` —— 接缝 §1 全部归一化类型 + `LLMUsage = Usage` 别名。
   - `novelforge/llm/errors.py` —— `LLMError` 树（接缝 §1.11）。
   - `novelforge/llm/tiers.py` —— `ModelTier`(FAST/MID/STRONG + 别名) / `normalize_tier`。
   - `novelforge/tools/errors.py` —— `ToolError` 树。
   - 测试：`tests/llm/test_types_seam.py`、`test_errors_seam.py`、`tests/tools/test_errors_seam.py`。

2. **接入层支撑模块**：`llm/config.py`（key 仅 env）→ `llm/pricing.py` → `llm/cache.py` → `llm/tool_fallback.py` → `llm/stream.py` → `llm/structured.py` → `llm/provider.py`（协议）。

3. **Provider 实现**：`llm/providers/fake.py`（**测试核心，先写**）→ `llm/providers/__init__.py`（`build_providers` 工厂，缺 SDK 跳过）→ `anthropic.py` / `openai_compat.py` / `local.py`（SDK 可选，能力探测）。

4. **Gateway**：`llm/gateway.py`（档→model / `degrade_plan` / `classify_error` / `sleep_backoff` / 回退链 / 记账 / 缓存告警）。测试：`tests/llm/test_gateway*.py`。

5. **工具层支撑**：`db/connection.py`（`connect`/`init_db`/FTS 重建）→ `contracts.py`（`BibleChangeProposal`/`StateTransition` 等）→ `tools/dedup.py` → `tools/truncate.py` → `tools/audit.py`（含 `tool_call_log` DDL 追加进 `db/schema.sql`）→ `tools/registry.py` → `tools/context.py`。

6. **6 个内置只读工具**：`tools/builtin/{world_state,facts,entities,foreshadow,recall,continuity}.py` + `__init__.register_builtin_tools`。确定性 SQL，逐列对照 §02 DDL。测试：`tests/tools/test_builtin.py`（每工具正例/空/边界）。

7. **工具循环**：`tools/loop.py`（`ToolLoop`/`ToolLoopResult`）。测试：`tests/tools/test_loop.py`（FakeProvider 脚本驱动全分支）。

8. **Skill 落地**：`skills/chapter_draft.py`（`ChapterDraftSkill` 用 `ToolLoop` 跑受限 ReAct）。测试：`tests/skills/test_chapter_draft.py`。

9. **契约测试（守接缝不漂移，全程跑）**：`tests/test_seam_contract.py`（无 import 环、接缝类型各只定义一处、`FakeProvider` 满足协议、Gateway 签名匹配、ToolLoop 只调 `gateway.generate`）。

> 外部依赖纪律：`anthropic`/`openai`/`httpx`/`jieba` 全部**可选**——能力探测 + 缺失回退/跳过，零外网零 SDK 即可 import 与单测。真实供应商调用仅在装了 SDK + 设了 env key 时才走（集成测试标 `@pytest.mark.integration`，默认 `-m "not integration"` 跳过）。

---

## 与 MVP 路线（§09）的关系

- **受限工具循环（§12）属 MVP1，不属 MVP0。** §12 的 6 个工具里 `query_world_state` 依赖 `get_world_state(as_of=N)` 投影、`peek_continuity` 依赖确定性 validator 与 as-of 投影——而 **as-of 投影 `get_world_state` 与 beat sheet 规划在 §09 中明确属 MVP1**（MVP0 不做）。因此整条受限 ReAct 工具循环随 MVP1 一起落地。
- **MVP0 可先用"单次 provider 调用 + Echo/FakeProvider"。** MVP0 只需接入层最小路径：`LLMGateway.generate(...)` 单次调用 + `FakeProvider`（脚本化）/ Echo provider 即可驱动"Plan→Recall→Draft→Check→Revise→Gate→Commit"骨架的正文创作，无需工具循环；canon 全人工维护、不做 LLM 抽取入库（§09 §9.1）。这样 §14 接入层在 MVP0 即可单独立起、单独验收。
- **演进路径**：MVP0 单次调用 → MVP1 接入 `ToolLoop`（Draft/Check 内部有界 ReAct）+ as-of 投影 + LLM 辅助抽取入 staging → MVP2 接入 embedding/RRF/`scene_vec`（`get_recall_pack` 的向量补充从确定性章节就近退化升级为真实 KNN）→ MVP3 全自动。固定管线骨架自 MVP0 起不变；只有 Draft/Check 内部从"单次"升级为"有界循环"。
- **硬原则贯穿全阶段**：HP1（软记忆 RAG / 硬状态确定性 SQL）、HP2（canon 只追加、LLM 只产 proposal）、HP9（`tool_call_log`/`skill_run_log`/`promotion_log` append-only 审计）、HP10（稳定前缀不污染、STRONG 只留正文与复核、断路器）、HP11（单一 SQLite 真相源、索引可重建、本地优先）在三篇规格中均有对应实现点与测试断言。

---

## 接缝核验摘要（写规格时已逐项核对）

- **接缝一致**：§12 与 §14 两份规格头部均显式声明"接缝四件套只 import 不重定义"，并逐字列出导入清单；`ToolLoop` 唯一调 `LLMGateway.generate(tier, messages, system, tools, cache_hint, ...)`，与接缝 §2.2 / §14.9.5 签名逐字一致；`Response{text, tool_calls:[ToolCall], usage, stop_reason}` 字段在两节一致；终止判据统一为"`resp.tool_calls` 空即停"（`stop_reason` 仅辅助）。
- **schema 列正确**：§12 六个工具的 SQL 所用表名/列名均在 §02 DDL 中真实存在（`character_power_log`/`knowledge_edges`/`item_log`/`item_ownership`/`numeric_facts`/`facts`/`facts_fts`/`entities`/`entity_aliases`/`foreshadow`/`power_ranks`/`timeline_events`/`geo_locations`/`l2_scenes` 逐列核对通过）；`foreshadow.state` 枚举以 §02 真实 DDL（`planted/reinforced/misled/paid_off/overdue`，无 `abandoned`）为准，规格已就设计稿 §12.3 表格的 `abandoned` 笔误做了显式纠正说明。
- **签名/异常/边界完备**：异常双根（`LLMError`/`ToolError` 互不继承）、错误处置分工表、四类终止条件（`final_output`/`no_progress`/`max_steps`/`budget`）、优雅降级"绝不丢章"均在两节闭环；`tool_call_log` DDL（含 `provider`/`model`/`note`）与 append-only 触发器齐备。

> 已知需在编码前补齐的接缝缺口（不阻断阅读，但影响 §07 兼容）：接缝把 `Usage.usd()` 改为 `Usage.usd(pricing)`（需参数）、`billable_tokens()` 改名 `billable()`，而 §07 设计稿的 `BudgetLedger.charge` 仍以无参 `usage.usd()` / `usage.billable_tokens()` 调用——`LLMUsage = Usage` 别名不能自动消解此签名差异。落地时需在 `novelforge/skills/budget.py` 的 `charge(usage, pricing)` 上同步调整（由 Gateway 在 `charge` 调用点传入 `pricing_for(pc, tier)`），并在接缝篇补一句"`BudgetLedger.charge` 签名随之更新"。详见各篇正文与本目录评审记录。
