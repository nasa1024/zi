## 12. Agent循环与工具调用（受限ReAct）

本节落实**决定 1** 的微观一半：宏观仍是固定的确定性管线 `Plan→Recall→Draft→Check→Revise→Gate→Commit`（详见第 7 节），但在 **Draft / Check 两个阶段内部**，允许 LLM 在一个**受限只读工具集**上做**有界 ReAct 循环**——最多 K 步、按需补取上下文、用完即停。它既不是"一次性单轮调用"（会漏召回、写错状态），也不是"全自主开放 Agent"（不可审计、烧预算、可能写 canon）。

本节定义：受限循环的形态与边界（§12.1）、`ToolRegistry` 与工具契约（§12.2）、MVP 只读工具集（§12.3）、循环控制与终止/收尾（§12.4）、确定性与可审计（`tool_call_log`，§12.5）、缓存纪律（§12.6）、与厂商无关工具调用的衔接（§12.7）、`ChapterDraftSkill` 端到端伪代码与示例 trace（§12.8）。

> 命名严格对齐既有术语：`Orchestrator` / `Skill Registry` / `SkillContext` / `LLMGateway` / `LLMProvider` / `ToolRegistry` / `RestrictedWorkspace` / `PromotionPolicy` / `Route` / `get_world_state` / `review_queue` / `fact_candidates` / `promotion_log`。本节新增持久化表仅一张：`tool_call_log`。
>
> 跨节边界：宏观管线与各 Skill 契约详见第 7 节；World State 各表、`get_world_state(as_of_chapter=N)` 投影与确定性 validator 详见第 2、4 节；治理闸门与草稿层/canon 层隔离详见第 3、7 节；**厂商无关 Provider 抽象、跨厂商 tool_calls 与结构化输出归一化、能力降级详见第 14 节**——本节只**消费** §14 已归一化的 `tool_calls`，不重复 Provider 细节。

---

### 12.1 宏观管线 vs 微观循环：关系与边界

固定管线是"生产线"，受限工具循环是 Draft/Check 两个工位内部的"按需取料"。管线阶段、阶段顺序、阶段间的闸门**全部固定且确定**；只有在 Draft 起草与 Check 校验时，LLM 才被允许在已声明的只读工具上做有界 ReAct——发现自己缺一块上下文（某角色当前境界？某伏笔到期没？），就调一次工具补取，再继续，最多 K 步。

```mermaid
flowchart TD
    subgraph MACRO["宏观：固定确定性管线（第 7 节 Orchestrator，顺序与闸门均固定）"]
        P[Plan] --> R[Recall] --> D[Draft] --> C[Check<br/>continuity ∥ craft] --> RV[Revise ≤N] --> G[Gate<br/>PromotionPolicy] --> CM[Commit]
        RV -. 必重跑 .-> C
    end

    subgraph MICRO["微观：仅 Draft / Check 内部有受限 ReAct 循环（本节）"]
        direction TB
        S0([进入阶段：注入稳定前缀 system]) --> S1{LLM：是否需要补取上下文?}
        S1 -- 是 --> S2[发出归一化 tool_calls<br/>§14 已抹平厂商差异]
        S2 --> S3[ToolRegistry 执行<br/>确定性 SQL only / read_only]
        S3 --> S4[breaker.guard 预算检查<br/>+ 去重缓存 + observation 截断]
        S4 --> S1
        S1 -- 否：产出最终结构化输出 --> S5([正文 + BibleChangeProposal[] + state_transitions[]])
    end

    D -. Draft 内部就是这个循环 .-> MICRO
    C -. ContinuityCheck/CraftCheck 内部同样可用 .-> MICRO
    S3 -. 工具绝不写 canon；写只能产 BibleChangeProposal 经 Gate .-> G
```

边界铁律（贯穿本节）：

1. **循环只在 Draft / Check 内部**。Plan/Recall/Gate/Commit 没有 ReAct——它们是确定性编排与治理动作。Recall 的 as-of 投影 + 实体优先召回（第 7 节 §7.5）已把"主料"备齐；工具循环只补"主料没覆盖到的边角料"，不替代 Recall。
2. **工具只读**。MVP 工具集全部 `read_only=True`，回答事实类查询一律走确定性 SQL（**硬原则 1**）。LLM 只决定"何时调、调哪个、传什么参"，不决定事实本身。
3. **工具绝不写 canon**（**硬原则 2**）。任何对世界状态的"修改意图"只能由 Skill 最终产出 `BibleChangeProposal[]` / `state_transitions[]`，交 **Gate（PromotionPolicy）** 决定去向（`Route.COMMIT/REVIEW/HOLD/REJECT`，第 7 节 §7.7）。循环内不存在 write 工具。
4. **有界**。`max_tool_steps`（config，默认 6）封顶，每步 `breaker.guard()`（第 7 节 §7.6），超步/超预算优雅收尾（§12.4）。
5. **可审计**。每次工具调用 append 一条 `tool_call_log`（**硬原则 9**，§12.5）。

---

### 12.2 ToolRegistry 与工具契约

`ToolRegistry` 是与 `Skill Registry` 平级的进程内单例（FastAPI lifespan 注册），负责：登记工具、向 LLM 暴露工具定义（JSON Schema）、按 Skill 的 `read_scopes` 过滤可见工具、执行 handler、记账与写 `tool_call_log`。它**不**关心厂商——拿到的是 §14 归一化后的 `tool_calls`，吐回的是归一化的 tool result（§12.7）。

#### 12.2.1 工具契约

```python
# control_plane/tool_registry.py
from __future__ import annotations
from typing import Any, Callable, Literal
from pydantic import BaseModel, Field

class ToolSpec(BaseModel):
    """单个工具的契约。read_only 是安全护栏的核心标志。"""
    name: str                              # 全局唯一，如 "query_world_state"
    description: str                       # 给 LLM 看的用途说明（何时该调）
    input_schema: dict[str, Any]           # JSON Schema 入参（直接喂给各 Provider）
    handler: Callable[["ToolContext", dict], "ToolResult"]  # 纯函数，确定性 SQL
    read_only: bool = True                 # MVP 一律 True；False 需治理审批，禁用于自动循环
    cost_hint: "ToolCost" = Field(default_factory=lambda: ToolCost())  # 预算估计
    scope: str                             # 归属 read_scope，按 Skill 白名单过滤可见性
    cacheable: bool = True                 # 相同 args 在同一 run 内可复用结果（§12.4 去重）

class ToolCost(BaseModel):
    """供 breaker 预扣与循环成本预测；SQL 工具开销主要是 observation token。"""
    est_sql_ms: int = 5                    # 典型 SQLite 查询耗时（毫秒级，索引命中）
    est_result_tokens: int = 400           # observation 预估 token（用于预算与截断）

class ToolContext(BaseModel):
    """注入 handler 的只读句柄。复用 SkillContext 的 repos / as_of，绝不给裸 conn 写权限。"""
    project_id: str
    as_of_chapter: int                     # = SkillContext.as_of_chapter（投影基准）
    repos: "RepositoryBundle"              # 只读 Repository 句柄（第 7 节）
    workspace: "RestrictedWorkspace"       # canon 对自动 Skill 只读（第 7 节 §7.8）

class ToolResult(BaseModel):
    ok: bool
    content: Any                           # 结构化结果（dict/list），交给 §12.4 序列化+截断
    result_digest: str = ""               # content 的稳定摘要（sha256 前 16，用于审计/去重键）
    note: str = ""                         # 空结果/降级说明（如 "no_open_foreshadow"）
```

#### 12.2.2 注册与暴露 API

```python
class ToolRegistry:
    _tools: dict[str, ToolSpec]

    def register(self, spec: ToolSpec) -> None:
        assert spec.name not in self._tools, f"duplicate tool: {spec.name}"
        assert spec.read_only, "MVP: 自动循环只允许 read_only 工具（硬原则 1/2）"
        self._tools[spec.name] = spec

    def visible_for(self, skill: "SkillContract") -> list[ToolSpec]:
        """按 Skill 的 read_scopes 过滤——Skill 只看得到它有权读的工具。"""
        return [t for t in self._tools.values() if t.scope in skill.read_scopes]

    def tool_definitions(self, skill: "SkillContract") -> list[dict]:
        """产出喂给 §14 Provider 的工具定义（name/description/input_schema）。
        这部分内容稳定 → 进缓存稳定前缀（§12.6）。"""
        return [{"name": t.name, "description": t.description,
                 "input_schema": t.input_schema} for t in self.visible_for(skill)]

    def execute(self, name: str, args: dict, tctx: ToolContext) -> ToolResult:
        spec = self._tools[name]
        # 二次护栏：执行期再确认只读（防止注册后被改写）
        if not spec.read_only:
            raise PermissionError(f"tool {name} is not read_only; refused in auto loop")
        return spec.handler(tctx, args)     # handler 内部纯 SQL，无 LLM
```

**契约要点**：`read_only` 是安全分水岭——`ToolRegistry.register` 与 `execute` 双重拒绝非只读工具进自动循环；`scope` 让工具可见性绑定到 Skill 的 `read_scopes`（与第 7 节受限 workspace 同一套白名单哲学）；`cost_hint` 给 `breaker` 做预扣与循环成本预测；`input_schema` 直接复用为各 Provider 的 tool 定义（§14 负责把它转成各厂商的 schema 方言）。

---

### 12.3 MVP 只读工具集

下表是 MVP 必备的 6 个工具，**全部 `read_only=True`、全部确定性 SQL**。它们都从 World State Store / 工艺层 / 检索层读，**绝不写任何表**；它们的语义基准统一是 `ToolContext.as_of_chapter`（= 目标章 - 1），与 Draft 的写时约束（**硬原则 3**）严格对齐，杜绝"工具读到未来章"的时序泄漏。

| 工具 name | 用途（description 摘要） | 入参（JSON Schema 要点） | 底层确定性查询 | scope |
|---|---|---|---|---|
| `query_world_state` | 取某 as-of 章的世界状态投影切片（境界/物品/知情/数值/位置） | `as_of`(int, 默认=ctx.as_of)、`entity_id?`、`facets?[]`（power/item/knowledge/numeric/geo） | `get_world_state(as_of_chapter=as_of)`（§4.4），按 entity/facet 切片 | `world_state` |
| `search_facts` | 按实体或关键词检索已 canon 化的事实 | `entity?`、`keyword?`、`limit`(默认 8) | `facts WHERE status='canon'` + `facts_fts`（FTS5+jieba）补充，RRF 合并 | `facts` |
| `lookup_entity` | 别名→canonical 实体归一（消歧） | `alias`(str) | `entity_aliases JOIN entities`（精确+前缀） | `entities` |
| `get_open_foreshadow` | 列当前未回收/到期伏笔 | `as_of?`、`include_overdue`(默认 true) | `foreshadow WHERE status NOT IN ('paid_off','abandoned') AND planted_chapter<=as_of` | `foreshadow` |
| `get_recall_pack` | 取某实体/beat 的结构化召回包（事实+场景） | `entity_id?`、`keywords?[]`、`k`(默认 8) | 实体优先 SQL + `facts_fts`/`scene_vec` 补充（第 6 节召回逻辑封装） | `recall` |
| `peek_continuity` | 对**局部草稿片段**跑确定性 validator，预检冲突 | `draft_fragment`(str)、`as_of?` | `extract_claims(fragment)` → 第 4 节 hard validators（境界/时序/库存/金手指/信息差） | `continuity` |

```python
# control_plane/tools/world_state.py（query_world_state 示例 handler）
def _query_world_state(tctx: ToolContext, args: dict) -> ToolResult:
    as_of = args.get("as_of", tctx.as_of_chapter)
    # 时序护栏：工具绝不读未来章（硬原则 3）
    as_of = min(as_of, tctx.as_of_chapter)
    world = tctx.repos.world_state.project(as_of_chapter=as_of)   # = get_world_state(§4.4)
    facets = args.get("facets") or ["power", "item", "knowledge", "numeric", "geo"]
    eid = args.get("entity_id")
    slice_ = world.slice(entity_id=eid, facets=facets)            # 纯字典切片，无 LLM
    return ToolResult(ok=True, content=slice_.model_dump(),
                      result_digest=_digest(slice_), note="" if slice_ else "empty")
```

```python
# control_plane/tools/continuity.py（peek_continuity 示例 handler）
def _peek_continuity(tctx: ToolContext, args: dict) -> ToolResult:
    as_of = min(args.get("as_of", tctx.as_of_chapter), tctx.as_of_chapter)
    world = tctx.repos.world_state.project(as_of_chapter=as_of)
    claims = extract_claims(args["draft_fragment"])              # 第 4 节
    # 跑 hard validators（确定性），不跑 LLM-judge——预检只看硬冲突
    issues = run_hard_validators(claims, world, tctx.repos.conn_ro)  # 第 4 节 validator 集
    return ToolResult(ok=True,
                      content={"hard_issues": [i.model_dump() for i in issues]},
                      result_digest=_digest(issues),
                      note="clean" if not issues else f"{len(issues)} hard_issue")
```

设计要点：

- **所有事实类回答都是确定性 SQL**（**硬原则 1**）：`query_world_state`/`search_facts`/`lookup_entity`/`get_open_foreshadow`/`get_recall_pack` 的真值来自关系表与 FTS5，**LLM 只决定何时调用、传什么参**，不参与事实判定。
- **`peek_continuity` 是循环内的"写时自检"**：它让 Draft 在产出整段正文前，先对一小段草稿跑第 4 节的 hard validators（如发现"叶凡突破金丹但上一章还是筑基且无合法迁移"），自我纠偏——把第 7 节"事后 Check"的一部分前移成"写时约束"（呼应**硬原则 3**），但**绝不阻断**、**绝不写状态**，仅返回 issue 供 LLM 自我修正。
- **工具绝不写 canon**（**硬原则 2**）：表中无任何 `op=write` 工具。LLM 想"改设定"，只能让 Skill 最终把意图编码进 `BibleChangeProposal[]`，由 Gate 决定（§12.1 铁律 3）。
- **实体优先**（**硬原则 4**）：`get_recall_pack`/`search_facts` 都以结构化实体 SQL 为主、FTS5/向量为补充，与第 6 节召回策略一致，避免纯语义召回漏掉硬实体。

---

### 12.4 循环控制：有界、去重、截断、终止、收尾

受限 ReAct 循环的全部控制逻辑封装在 `ToolLoop`，被 Draft/Check 类 Skill 复用。它吃 `LLMGateway`（§14 多供应商版）、可见工具集、稳定前缀，吐最终结构化输出。

```python
# control_plane/tool_loop.py
class ToolLoopResult(BaseModel):
    final_output: dict          # 结构化产出：text + bible_change_proposals + state_transitions
    steps_used: int
    stopped_reason: Literal["final_output", "max_steps", "budget", "no_progress"]
    tool_calls: list[dict]      # 本轮所有工具调用摘要（已写 tool_call_log）

class ToolLoop:
    def __init__(self, *, gateway: "LLMGateway", registry: ToolRegistry,
                 breaker: "CircuitBreaker", cfg_max_steps: int = 6,
                 obs_token_budget: int = 6000):
        self.gw = gateway
        self.registry = registry
        self.breaker = breaker
        self.max_steps = cfg_max_steps           # config.pipeline.max_tool_steps（默认 6）
        self.obs_token_budget = obs_token_budget # observation 累计 token 上限
        self._cache: dict[str, ToolResult] = {}  # (tool,args) -> result，run 内去重缓存
        self._obs_tokens = 0

    def run(self, *, skill: "SkillContract", tier, system_stable: str,
            user_dynamic: str, tctx: ToolContext, ctx: "SkillContext") -> ToolLoopResult:
        tools = self.registry.tool_definitions(skill)   # 进稳定前缀（§12.6）
        messages = [{"role": "user", "content": user_dynamic}]
        calls_summary: list[dict] = []

        for step in range(1, self.max_steps + 1):
            self.breaker.guard()                        # ① 每步预算/断路检查（第 7 节 §7.6）

            resp = self.gw.call(                         # ② §14：归一化的多供应商调用
                tier=tier, system_stable=system_stable, tools=tools,
                messages=messages, cache_prefix=True)   # 稳定前缀走 1h cache（§12.6）

            # ③ 终止条件：模型不再要工具、给出最终结构化输出 → 立即停
            if not resp.tool_calls:
                final = parse_structured_output(resp)   # text+BibleChangeProposal[]+state_transitions[]
                return ToolLoopResult(final_output=final, steps_used=step,
                                      stopped_reason="final_output",
                                      tool_calls=calls_summary)

            # ④ 执行本步所有 tool_calls（§14 已抹平厂商差异，见 §12.7）
            tool_results = []
            progressed = False
            for call in resp.tool_calls:                 # call: {id, name, args}
                key = _dedup_key(call["name"], call["args"])
                if key in self._cache:                   # ⑤ 重复调用去重 → 复用缓存
                    res = self._cache[key]
                    note = "cache_hit"
                else:
                    res = self.registry.execute(call["name"], call["args"],
                                                tctx)     # 确定性 SQL
                    self._cache[key] = res
                    progressed = True
                    note = "fresh"
                # ⑥ observation 截断 + token 预算：易变内容，绝不进前缀（§12.6）
                obs, used = truncate_observation(res.content, self.obs_token_budget - self._obs_tokens)
                self._obs_tokens += used
                tool_results.append({"tool_call_id": call["id"], "content": obs})
                # ⑦ append-only 审计（硬原则 9，§12.5）
                write_tool_call_log(ctx, skill, step, call, res, note)
                calls_summary.append({"step": step, "tool": call["name"],
                                      "digest": res.result_digest, "note": note})

            # ⑧ 无进展保护：整步全是缓存命中（无新信息）→ 提前收尾，防原地打转
            if not progressed:
                final = self._force_finalize(resp, messages, system_stable, tier)
                return ToolLoopResult(final_output=final, steps_used=step,
                                      stopped_reason="no_progress",
                                      tool_calls=calls_summary)

            # 把归一化 tool_results 回填进对话（§14 负责转回各厂商 message 形态）
            messages.append({"role": "assistant", "tool_calls": resp.tool_calls})
            messages.append({"role": "tool", "results": tool_results})

        # ⑨ 超步优雅收尾：用已取到的上下文强制产一次最终结构化输出（不再给工具）
        final = self._force_finalize_no_tools(messages, system_stable, tier)
        return ToolLoopResult(final_output=final, steps_used=self.max_steps,
                              stopped_reason="max_steps", tool_calls=calls_summary)
```

五项控制机制：

1. **`max_tool_steps` 封顶**（config，默认 6）：`config.pipeline.max_tool_steps`。网文连载多数情况下 2–4 步足够补齐；6 步是上限护栏，不是目标。
2. **每步 `breaker.guard()`**：复用第 7 节 §7.6 断路器，超预算立即 `CircuitTripped`，由 Skill 捕获后走收尾（下条）。工具的 `cost_hint.est_result_tokens` 也参与预测——若预测下一步会超 `obs_token_budget`，提前停。
3. **重复调用去重 + 结果缓存**（`_dedup_key`）：同一 run 内 `(tool, 归一化 args)` 命中缓存直接复用，**不重复打 SQL、不重复计 observation token**。`cacheable=False` 的工具（如未来的时间敏感工具）跳过缓存。
4. **observation 截断与 token 预算**：`obs_token_budget`（默认 6000）封住"工具结果把上下文撑爆"。`truncate_observation` 按结构化优先级裁剪（保实体/数值/issue，截长文本），并标注 `[truncated]`。
5. **终止与收尾**：
   - 正常终止：模型产出**最终结构化输出**（正文 + `BibleChangeProposal[]` + `state_transitions[]`）即停（`stopped_reason="final_output"`）。
   - 无进展：整步全缓存命中 → `_force_finalize`（`no_progress`）。
   - 超步/超预算：用**已取到**的上下文强制产一次结构化输出、**不再给工具**（`max_steps` / `budget`）——**绝不整章丢弃**，正文照常落 L0，未通过硬校验的状态变更一律不晋升（第 7 节 §7.6.3 未决账本 + §7.7 草稿/canon 隔离），并在产物上打 `degraded_by=tool_loop_<reason>` 标记供审计与人审。

> 收尾与第 7 节强一致：超步/超预算属于"优雅降级"，不抛错给用户；残留风险通过"先进草稿层、Gate 不放行高风险" 兜底（**硬原则 5/8**）。

---

### 12.5 确定性与可审计：`tool_call_log`

每次工具调用 append 一条 `tool_call_log`（**硬原则 9**）。它与 `skill_run_log`（第 7 节）/ `promotion_log`（第 3 节）/ workspace 审计共同构成 append-only 证据链：任一章可被复盘"它在 Draft 时查了什么、查到的摘要是什么、花了多久"。

```sql
-- 控制平面：工具调用审计（append-only；与 Memory/Governance 同库同事务）
CREATE TABLE IF NOT EXISTS tool_call_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT    NOT NULL,        -- = skill_run_log.run_id（关联到具体 Skill 执行）
    chapter        INTEGER NOT NULL,
    skill          TEXT    NOT NULL,        -- skill_name@version
    step           INTEGER NOT NULL,        -- ReAct 第几步（1..max_tool_steps）
    tool_name      TEXT    NOT NULL,
    args_json      TEXT    NOT NULL,        -- 归一化入参（json_valid CHECK）
    result_digest  TEXT    NOT NULL,        -- content 的 sha256 前 16；不存全量结果（省空间/隐私）
    latency_ms     INTEGER NOT NULL,        -- handler 执行耗时
    provider       TEXT,                    -- 本步 LLM 决策所用供应商（第 14 节 LLMProvider；纯本地工具步可空）
    model          TEXT,                    -- 本步所用模型 ID（第 14 节语义档→模型映射）
    note           TEXT,                    -- fresh / cache_hit / empty / degraded
    ts             TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (json_valid(args_json))
);
CREATE INDEX IF NOT EXISTS idx_tcl_run    ON tool_call_log(run_id, step);
CREATE INDEX IF NOT EXISTS idx_tcl_chap   ON tool_call_log(chapter, tool_name);
```

```python
def write_tool_call_log(ctx, skill, step, call, res: ToolResult, note: str) -> None:
    ctx.repos.audit.append_tool_call(
        run_id=ctx.run_id, chapter=ctx.target_chapter,
        skill=f"{skill.name}@{skill.version}", step=step,
        tool_name=call["name"], args_json=json.dumps(call["args"], ensure_ascii=False),
        result_digest=res.result_digest, latency_ms=res.latency_ms, note=note)
```

**工具本身可单测**（与第 7 节 §7.1 "Skill 可单测" 同一工程理念）：因为每个 handler 是**纯 SQL、无 LLM**，单测只需喂 fixture 数据库 → 断言 `ToolResult.content`。例如 `query_world_state(as_of=210, entity_id='ent_yefan')` 在给定 fixture 上必然返回确定结果——这正是**硬原则 1**（硬状态走关系表）带来的可测性红利：循环的"事实层"完全确定、可回归，不确定性只剩 LLM 的"何时调"，而后者由 `tool_call_log` 留痕、可审计。

---

### 12.6 缓存纪律（硬原则 10）

工具循环是 prompt cache 的高危区——每步都在往对话里追加易变的 observation，极易污染稳定前缀。本节的纪律与第 7 节 §7.5/§7.6.2 完全一致：**稳定前缀进 1h cache、observation 进可变区，二者物理隔离**。

| 区域 | 内容 | 缓存策略 |
|---|---|---|
| **稳定前缀**（`system_stable`） | bible 渲染视图 + 风格约束 + 否定型禁忌 + **工具定义（`tool_definitions`）** | 进 1h prompt cache（`cache_prefix=True`）；同一项目跨章复用 |
| **可变区**（`messages`） | beat sheet、动态召回、**每步 tool_calls / tool observation** | 绝不进前缀；按章、按步变化 |

铁律：

1. **工具定义进稳定前缀**：`ToolRegistry.tool_definitions(skill)` 对同一 Skill 稳定不变（除非工具集变更），与 bible/风格/约束一起构成稳定前缀，享受缓存命中。
2. **observation 绝不进前缀**：tool result 是本节头号 silent cache invalidator——它每步都变、带章节号/数值/issue。它只能进 `messages` 可变区。`ToolLoop.run` 里 `system_stable` 在整个循环内**逐字不变**，所有变化都堆在 `messages`。
3. **命中验证**：复用第 7 节 §7.6.2——`LLMGateway` 用 `cache_read_input_tokens > 0` 验证前缀命中；循环里**每一步**都应命中同一前缀（首步可能 miss，后续步必命中），若中途突然 miss → 告警"前缀被污染"。
4. **Opus/STRONG 档只留正文与复核**（**硬原则 10**）：工具的事实查询是确定性 SQL（零 LLM 成本）；昂贵的 STRONG 档算力只花在正文创作与冲突复核上，绝不用 STRONG 去"想要不要查境界"——那是 FAST/MID 档就能决策的轻动作（档位语义见第 14 节）。

---

### 12.7 与厂商无关工具调用的衔接

本节的循环**消费** §14 归一化后的 `tool_calls`，不关心底层是 Anthropic、OpenAI 兼容（含 vLLM/网关）还是本地（ollama/llama.cpp）。三者的工具调用协议差异（字段名、结构、是否原生支持 tool use）全部由第 14 节的 `LLMProvider` 抹平：

```python
# §14 归一化契约（本节只消费，定义见第 14 节）
class NormalizedToolCall(BaseModel):     # gateway.call(...).tool_calls 的元素
    id: str
    name: str
    args: dict                           # 已解析为 dict（不论厂商是 JSON 字符串还是 object）

# 本节回填 tool result 时也用归一化结构，由 §14 转回各厂商 message 形态：
#   {"role": "tool", "results": [{"tool_call_id": ..., "content": <obs>}]}
```

衔接职责划分：

- **§14 负责**：把 `ToolRegistry.tool_definitions` 转成各厂商 tool schema；把各厂商返回的 tool 调用统一成 `NormalizedToolCall`；把本节的归一化 tool_results 转回各厂商 message；对**不支持原生 tool use** 的本地模型，§14 用"提示词模拟 + 结构化解析"降级实现同一接口（能力降级，决定 3）。
- **本节负责**：循环控制（步数/预算/去重/截断/终止）、工具执行（确定性 SQL）、审计（`tool_call_log`）。

因此本节伪代码里的 `self.gw.call(..., tools=tools, messages=messages)` 与 `resp.tool_calls` 是**厂商无关**的——换 Provider 不改本节一行。Provider 抽象、跨厂商结构化输出归一化、能力降级矩阵详见第 14 节。

---

### 12.8 `ChapterDraftSkill` 端到端伪代码与示例 trace

把以上拼起来：`ChapterDraftSkill`（第 7 节 §7.4，`trigger=draft`，`model_tier=opus`/STRONG）在 `run()` 内用 `ToolLoop` 做受限 ReAct，最终产 `text + BibleChangeProposal[] + state_transitions[]`。

```python
# skills/chapter_draft.py
class ChapterDraftSkill:
    contract = SkillContract(
        name="ChapterDraftSkill", version="1.2.0",
        trigger=SkillTrigger.DRAFT, model_tier=ModelTier.OPUS,
        inputs=[IOSpec(name="beat_sheet", schema_ref="craft.BeatSheet"),
                IOSpec(name="world_as_of", schema_ref="state.WorldState"),
                IOSpec(name="recall", schema_ref="recall.RecallPack"),
                IOSpec(name="stable_prefix", schema_ref="prompt.StablePrefix")],
        outputs=[IOSpec(name="text", schema_ref="draft.ChapterText"),
                 IOSpec(name="bible_change_proposals", schema_ref="gov.BibleChangeProposal", required=False),
                 IOSpec(name="state_transitions", schema_ref="state.StateTransition", required=False)],
        workflow="ReAct 受限循环：按需补取 as-of 状态/伏笔/连续性预检 → 产正文+fact diff+状态迁移",
        dod=[DoDCheck(code="covers_all_beats", description="覆盖 beat sheet 全部 beat", predicate_ref="dod_covers_all_beats"),
             DoDCheck(code="no_canon_write", description="只产 proposal，不直接改 bible", predicate_ref="dod_no_canon_write"),
             DoDCheck(code="transitions_legal_from_asof", description="state_transitions 须能从 as_of 合法迁移到达", predicate_ref="dod_transitions_legal")],
        # 可见工具集（绑定 read_scopes → ToolRegistry.visible_for 据此过滤）
        read_scopes=["world_state", "facts", "entities", "foreshadow", "recall", "continuity"],
        write_scopes=["drafts", "candidates"],   # canon 对它只读（第 7 节 §7.8）
        cache_prefix_keys=["bible_view", "style_constraints", "taboos", "tool_definitions"],
    )

    def run(self, ctx: SkillContext, *, beat_sheet, world_as_of, recall, stable_prefix) -> SkillResult:
        # ① 组装稳定前缀（进 1h cache）：bible/风格/约束 + 工具定义（§12.6）
        registry = ctx.tools                       # ToolRegistry 句柄
        system_stable = render_stable_prefix(
            bible_view=stable_prefix.bible_view, style=stable_prefix.style,
            taboos=stable_prefix.taboos,
            tool_defs=registry.tool_definitions(self.contract))   # 工具定义入前缀

        # ② 动态区（可变，不进前缀）：beat sheet + 本章动态召回 + 产出格式契约
        user_dynamic = render_draft_request(
            beat_sheet=beat_sheet, recall_dynamic=recall.dynamic_part(),
            as_of=ctx.as_of_chapter,
            output_contract="返回 JSON：{text, bible_change_proposals[], state_transitions[]}")

        # ③ 跑受限工具循环（§12.4）
        tctx = ToolContext(project_id=ctx.project_id, as_of_chapter=ctx.as_of_chapter,
                           repos=ctx.repos, workspace=ctx.workspace)
        loop = ToolLoop(gateway=ctx.llm, registry=registry, breaker=ctx.breaker,
                        cfg_max_steps=ctx.cfg.pipeline.max_tool_steps,        # 默认 6
                        obs_token_budget=ctx.cfg.pipeline.obs_token_budget)   # 默认 6000
        lr = loop.run(skill=self.contract, tier=self.contract.model_tier,
                      system_stable=system_stable, user_dynamic=user_dynamic,
                      tctx=tctx, ctx=ctx)

        # ④ 最终结构化产出（绝不写 canon——只产 proposal/transition，交 Gate 决定）
        out = lr.final_output
        return SkillResult(
            ok=True,
            outputs={"text": ChapterText(body=out["text"], degraded=lr.stopped_reason),
                     "bible_change_proposals": [BibleChangeProposal(**p) for p in out.get("bible_change_proposals", [])],
                     "state_transitions": [StateTransition(**s) for s in out.get("state_transitions", [])]},
            dod_report=[], usage=ctx.llm.last_usage(),
            issues=[] if lr.stopped_reason == "final_output" else [f"degraded:{lr.stopped_reason}"])
```

> 注：`out["text"]` 落 L0 草稿区、`bible_change_proposals`/`state_transitions` 进 Gate——`ChapterDraftSkill` 自身**不**碰 canon（第 7 节 §7.4/§7.7、**硬原则 2**）。DoD `transitions_legal_from_asof` 与循环内 `peek_continuity` 互补：前者是 Skill 返回前的强校验，后者是循环内的写时自检。

#### 12.8.1 示例 trace（叶凡突破金丹）

目标章 = 第 211 章；`as_of_chapter = 210`。beat sheet 要求"叶凡突破到下一境界并以新威压震慑对手"。`ChapterDraftSkill` 在 STRONG 档跑受限 ReAct：

```
run_id=run_211a  chapter=211  skill=ChapterDraftSkill@1.2.0
system_stable(命中 1h cache, cache_read_input_tokens=12483 >0 ✓):
  [bible 渲染视图 | 风格约束 | 否定型禁忌 | 工具定义×6]

step 1  LLM 推理："要写突破，先确认叶凡当前境界与可达上界"
  tool_call → query_world_state(as_of=210, entity_id="ent_yefan", facets=["power"])
  ToolRegistry.execute → get_world_state(210).slice(...)  [确定性 SQL]
  observation: {"power_rank":"金丹·初期", "next_rank":"金丹·中期", "log_ref":"cpl_2087"}
  tool_call_log: run_211a/211/ChapterDraftSkill@1.2.0/1/query_world_state
                 args={"as_of":210,"entity_id":"ent_yefan","facets":["power"]}
                 digest=9f3a1c..  latency_ms=4  note=fresh

step 2  LLM 推理："上一章已是金丹初期 → 本章合法迁移是 金丹·中期，不能跳级。
                  写突破到金丹中期，并预检这段会不会触发硬冲突。"
  tool_call → peek_continuity(draft_fragment="…叶凡周身灵气暴涨，金丹凝实，
                              一举踏入金丹中期…", as_of=210)
  ToolRegistry.execute → extract_claims + run_hard_validators  [确定性 SQL]
  observation: {"hard_issues": []}   note=clean  ← 无跳级、无时序冲突
  tool_call_log: …/2/peek_continuity  digest=00bb..  latency_ms=11  note=fresh

step 3  LLM 不再调工具 → 产最终结构化输出（stopped_reason=final_output, steps_used=3）
  final_output = {
    "text": "<第211章正文：叶凡金丹中期突破场景…>",
    "bible_change_proposals": [],     # 本章无设定变更
    "state_transitions": [
      {"entity_id":"ent_yefan", "facet":"power",
       "from":"金丹·初期", "to":"金丹·中期",
       "at_chapter":211, "evidence_refs":["draft:ch211#p3"]}
    ]
  }
```

后续（不在本节）：该 `state_transition` 进 **Check**（`ContinuityCheckSkill` 整章批量复核，第 7 节 §7.5）→ **Gate**（`PromotionPolicy`：power_system 属 `require_human_for`，**即便 auto 模式也走 `Route.REVIEW` 入 `review_queue`**，**硬原则 5/8**，第 7 节 §7.7）→ 人审通过后才 `commit_canon` 写 `character_power_log`（第 2 节）。

这条 trace 体现本节全部要点：**LLM 调 `query_world_state` 发现叶凡当前是金丹初期**（确定性 SQL 给事实，LLM 只决定何时调）→ **据此写"金丹中期"突破而非跳级**（写时约束，硬原则 3）→ `peek_continuity` 写时自检无冲突 → **产 `state_transition` 而非直接改状态**（硬原则 2）→ 每步 `tool_call_log` 留痕（硬原则 9）→ 全程稳定前缀命中、observation 在可变区（硬原则 10）。

---

### 12.9 config 片段

```yaml
# config.pipeline（第 8 节 config 根的 pipeline 段；本节新增三项）
pipeline:
  max_tool_steps: 6          # 受限 ReAct 循环步数上限（决定 1，建议默认 6）
  obs_token_budget: 6000     # 单次循环 observation 累计 token 上限（截断阈值）
  tool_dedup: true           # 同 run 内 (tool,args) 去重缓存
  # 工具循环只在 draft/check 阶段启用；plan/recall/gate/commit 无 ReAct
  tool_loop_phases: ["draft", "check"]
```

---

### 12.10 本节与其他节的衔接

- **宏观管线、Orchestrator、Skill 契约、断路器/预算、草稿层-canon 隔离、受限 workspace**：详见第 7 节（本节是其 Draft/Check 内部的微观循环）。
- **World State 各表、`get_world_state(as_of_chapter=N)` 投影、确定性 validator（`peek_continuity` 复用之）**：详见第 2、4 节。
- **召回策略（`get_recall_pack`/`search_facts` 的实体优先 + FTS5/向量补充）**：详见第 6 节。
- **治理闸门、`PromotionPolicy` / `Route` / `review_queue` / `fact_candidates` / `promotion_log`（state_transitions 与 BibleChangeProposal 的去向）**：详见第 3、7 节。
- **厂商无关 `LLMProvider` 抽象、跨厂商 `tool_calls` 与结构化输出归一化、能力降级、语义档位映射（FAST/MID/STRONG）**：详见第 14 节（本节只消费其归一化产物，不重复 Provider 细节）。
