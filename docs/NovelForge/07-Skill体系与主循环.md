## 7. Skill体系与主生成循环

本节定义 NovelForge 控制平面的两块核心：**Skill Registry**（与 Memory Core 平级的独立技能注册中心）与**主生成循环**（`Plan→Recall→Draft→Check→Revise→Gate→Commit`）。这是把"记忆/状态/工艺/治理"这些被动能力，组织成一条可运行、可断路、可审计的生产线的地方。

设计立场承接重设计第 12 项改动：**Skill System 从记忆 L4 拆出，作为与 Memory Core 平级的独立 Skill Registry**；记忆层只保留 L0 草稿 / L1 原子 / L2 场景三层（详见第 2 节）。Skill 不是记忆的一层，而是"读记忆/状态、调 LLM、产结构化产物、走治理"的可编排执行单元。控制平面 = `Orchestrator`（编排主循环）+ `Skill Registry`（技能契约与调度）。

---

### 7.1 为什么把 Skill 从记忆 L4 拆出

原 require.md 把 Skill 塞进记忆层级 L4，混淆了两件正交的事：

- **记忆/状态**是*被读写的数据*（数据平面：L0/L1/L2 + World State Store）。
- **Skill**是*读写这些数据的行为*（控制平面）。

把行为放进数据层级会导致三个问题：(a) "记忆有几层"和"系统有几个技能"被强绑定，加技能就像在改记忆架构；(b) Skill 无法独立拥有契约、版本、DoD（Definition of Done）、预算；(c) 主循环要调度的对象（Skill）和它操作的对象（Memory/State）混在一个命名空间里，编排逻辑无处安放。

拆分后的职责边界：

| 平面 | 组件 | 职责 | 本节涉及 |
|---|---|---|---|
| 控制平面 | `Orchestrator` | 跑主循环、管预算、断路、重试 | ✅ 7.4–7.7 |
| 控制平面 | `Skill Registry` | 注册/发现/调度 Skill，校验契约与 DoD | ✅ 7.2–7.3 |
| 数据平面 | `Memory Core` (L0/L1/L2) + `World State Store` | 被 Skill 读写 | 详见第 2 节 |
| 治理平面 | `Governance`（staging/晋升闸门/审计） | Gate/Commit 落点 | 详见第 3 节 |

**关键原则**：Skill 是无状态的纯函数式执行单元——所有持久状态都在 Memory Core / World State Store / Governance 表里；Skill 只通过受限 workspace（7.8）和 Repository 接口读写，**绝不**持有跨调用的内存状态。这样任一 Skill 可被独立单测（喂 fixture 状态 → 断言产物与 DoD），符合硬原则 7"确定性 validator 最易单测"的同一工程理念。

---

### 7.2 Skill 契约模板

每个 Skill 在注册时必须声明一份**契约**（contract）。契约是 Skill 的"接口 + 验收标准"，Orchestrator 只依赖契约、不依赖实现。

#### 7.2.1 契约字段定义

```python
# control_plane/skill_contract.py
from __future__ import annotations
from enum import Enum
from typing import Any, Callable, Literal
from pydantic import BaseModel, Field

class SkillTrigger(str, Enum):
    """Skill 在主循环中的触发位点。"""
    PLAN     = "plan"        # 规划阶段
    RECALL   = "recall"      # 召回阶段（一般由 Orchestrator 内置，不外挂）
    DRAFT    = "draft"       # 起草阶段
    CHECK    = "check"       # 校验阶段（continuity / craft）
    REVISE   = "revise"      # 修订阶段
    ON_DEMAND = "on_demand"  # 人工/事件触发，不在固定位点

class ModelTier(str, Enum):
    """成本分层：硬原则 10 —— Opus 只留正文与冲突复核。"""
    HAIKU  = "haiku"    # 抽取/去重/连续性初筛/格式化
    SONNET = "sonnet"   # 中等：beat sheet、风格改写、对白
    OPUS   = "opus"     # 正文创作、冲突复核

class IOSpec(BaseModel):
    """单个输入/输出端口的形状声明，绑定到 Pydantic 模型类名。"""
    name: str
    schema_ref: str                      # 对应 Pydantic 模型的全限定名
    required: bool = True
    description: str = ""

class DoDCheck(BaseModel):
    """Definition of Done：一条可程序判定的验收断言。"""
    code: str                            # 唯一标识，如 "beats_cover_all_chapters"
    description: str
    severity: Literal["blocker", "warn"] = "blocker"
    # 纯函数：吃 Skill 输出 + 上下文 → 通过/失败 + 说明
    predicate_ref: str                   # 注册在 DoD 校验器表里的函数名

class SkillContract(BaseModel):
    name: str                            # 全局唯一，如 "ChapterDraftSkill"
    version: str = "1.0.0"
    trigger: SkillTrigger
    model_tier: ModelTier                # 缺省调用的模型档
    inputs: list[IOSpec]
    outputs: list[IOSpec]
    workflow: str                        # 人类可读的工作流描述（步骤序列）
    dod: list[DoDCheck]                  # 验收标准；blocker 全过才算成功
    # 成本护栏（与 config.budget_per_chapter 叠加取小，见 7.6）
    max_tokens_per_call: int = 0         # 0=继承全局
    max_usd_per_call: float = 0.0
    # 受限 workspace 权限（见 7.8）
    read_scopes: list[str] = Field(default_factory=list)   # 可读路径/表白名单
    write_scopes: list[str] = Field(default_factory=list)  # 可写路径/表白名单
    # 缓存策略：稳定前缀复用（硬原则 10）
    cache_prefix_keys: list[str] = Field(default_factory=list)  # 进 1h prompt cache 的前缀来源
```

#### 7.2.2 Skill 执行接口

```python
# control_plane/skill_base.py
from typing import Protocol
from pydantic import BaseModel

class SkillContext(BaseModel):
    """Orchestrator 注入的运行上下文（只读句柄 + 预算账本）。"""
    project_id: str
    target_chapter: int
    mode: Literal["human_gate", "auto_promote", "hybrid"]
    as_of_chapter: int                   # World State 投影基准（=target_chapter-1）
    budget: "BudgetLedger"               # 见 7.6
    workspace: "RestrictedWorkspace"     # 见 7.8
    llm: "LLMGateway"                     # 带缓存/退避的 LLM 客户端，见 7.6
    repos: "RepositoryBundle"            # Memory/State/Governance 仓储句柄

class SkillResult(BaseModel):
    ok: bool
    outputs: dict[str, BaseModel]        # 按 contract.outputs[].name 索引
    dod_report: list["DoDOutcome"]       # 每条 DoD 的结论
    usage: "LLMUsage"                    # tokens / usd / cache_read_input_tokens
    issues: list[str] = []               # 非致命提示

class Skill(Protocol):
    contract: SkillContract
    def run(self, ctx: SkillContext, **inputs: BaseModel) -> SkillResult: ...
```

**DoD 是一等公民**：`run()` 返回前由 `Skill Registry` 强制对 `contract.dod` 逐条执行 `predicate_ref`，任一 `blocker` 不过 → `SkillResult.ok=False`。这把"Skill 算不算干完了"从口头约定变成可单测的程序断言，是 7.7 中"无 blocker 才放行"的微观基础。

---

### 7.3 注册机制

`Skill Registry` 是一个进程内单例 + 一张持久化的 `skill_registry` 表（记录已启用契约、版本、配置覆盖，便于审计"哪一章是哪个 Skill 哪个版本写的"）。

#### 7.3.1 注册表 DDL

```sql
-- 控制平面：Skill 注册记录（与 Memory/Governance 表同库同事务）
CREATE TABLE IF NOT EXISTS skill_registry (
    name              TEXT NOT NULL,
    version           TEXT NOT NULL,
    trigger           TEXT NOT NULL,         -- plan/draft/check/...
    model_tier        TEXT NOT NULL,         -- haiku/sonnet/opus
    contract_json     TEXT NOT NULL,         -- 序列化的 SkillContract
    enabled           INTEGER NOT NULL DEFAULT 1,
    registered_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (name, version)
);
-- 每章实际使用的 Skill 版本快照（审计：可复现/可追责）
CREATE TABLE IF NOT EXISTS skill_run_log (
    run_id            TEXT PRIMARY KEY,
    chapter           INTEGER NOT NULL,
    phase             TEXT NOT NULL,         -- plan/draft/check/revise
    skill_name        TEXT NOT NULL,
    skill_version     TEXT NOT NULL,
    model_tier        TEXT NOT NULL,
    ok                INTEGER NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    usd_cost          REAL NOT NULL DEFAULT 0.0,
    started_at        TEXT NOT NULL,
    finished_at       TEXT
);
```

#### 7.3.2 注册与发现 API

```python
# control_plane/skill_registry.py
class SkillRegistry:
    _skills: dict[str, Skill]                       # name -> 单例实例
    _by_trigger: dict[SkillTrigger, list[str]]      # 触发位点 -> name 列表

    def register(self, skill: Skill) -> None:
        """启动期登记。校验契约自洽，写 skill_registry 表。"""
        c = skill.contract
        assert c.name not in self._skills, f"duplicate skill: {c.name}"
        self._validate_contract(c)                  # IO schema 存在、DoD predicate 可解析
        self._skills[c.name] = skill
        self._by_trigger.setdefault(c.trigger, []).append(c.name)
        self._persist(c)

    def get(self, name: str) -> Skill: ...
    def for_trigger(self, t: SkillTrigger) -> list[Skill]: ...

    def invoke(self, name: str, ctx: SkillContext, **inputs) -> SkillResult:
        """统一入口：契约入参校验 → 预算预扣 → run → DoD 强校验 → 记账 → skill_run_log。"""
        skill = self.get(name)
        self._check_inputs(skill.contract, inputs)
        self._check_scopes(skill.contract, ctx.workspace)        # 受限 workspace 白名单
        res = skill.run(ctx, **inputs)
        res.dod_report = self._enforce_dod(skill.contract, res, ctx)
        if any(d.severity == "blocker" and not d.passed for d in res.dod_report):
            res.ok = False
        ctx.budget.charge(res.usage)                             # 见 7.6
        self._log_run(skill.contract, ctx, res)
        return res
```

注册发生在应用启动期（FastAPI lifespan），所有内置 Skill 显式 `register()`。第三方/实验 Skill 通过同一接口热插拔——只要满足 `Skill` 协议与契约自洽校验即可被 Orchestrator 调度，无需改动主循环代码。

---

### 7.4 核心 Skill 清单

下表给出 MVP 必备 Skill 及其契约要点。**所有 Skill 都遵守硬原则 1**：确定性事实/算术/时序/状态机一律走 SQLite + Python validator，Skill 内部**不得**用 LLM 做这类判断；LLM 只负责创作与软判断。

| Skill | trigger | model_tier | 输入（要点） | 输出（要点） | DoD（blocker 要点） |
|---|---|---|---|---|---|
| `PlannerSkill` | plan | sonnet | 卷目标、`chapter_cards`、当前 `pacing_state`、未决 `foreshadow` | 逐章 **beat sheet**（`beats`：payoff_beat/hook/value_shift/tension_point）作为 `ChapterDraftSkill` 的契约 | 每章≥1 hook 且有 value_shift；张力曲线不平坦；到期伏笔已排 payoff |
| `ChapterDraftSkill` | draft | **opus** | beat sheet、Recall 包（as-of 状态 + 实体召回）、稳定前缀（bible/风格/约束） | L0 章节草稿 + 结构化 `BibleChangeProposal[]` + 状态迁移声明 | 覆盖 beat sheet 全部 beat；产出结构化 fact diff 而非改写 bible；字数达标 |
| `SceneExpandSkill` | draft | sonnet | 某 beat、场景设定、`scene_vec` 相似桥段（可选） | 扩写后的场景文本（L0 局部） | 不引入未声明实体；不越级使用能力 |
| `ContinuityCheckSkill` | check | haiku→opus | 草稿、`as_of(N)` 投影、claim 列表 | hard issues（确定性 validators）+ soft issues（LLM-judge） | 双流水线均跑完；hard validator 结果可解释（带证据指针） |
| `CraftCheckSkill` | check | sonnet | 草稿、beat sheet、`pacing_state` | 工艺评分（hook 强度/value_shift/tension/pacing）+ 改进点 | 给出各 beat 的命中判定；与 continuity 并行无依赖 |
| `StyleRewriteSkill` | revise | sonnet | 草稿、`character_cards.voice_profile`、风格约束 | 改写后草稿（保语义改文风） | 不改动任何已校验的 fact/状态；只动表达层 |
| `ForeshadowingSkill` | plan / check | haiku | 草稿、`foreshadow` 表（planted→…→overdue） | 伏笔抽取/状态推进提案 + overdue 扫描 | overdue 检测走 SQL 而非 LLM；提案不直接写 canon |
| `CharacterDialogueSkill` | draft | sonnet | 角色 `voice_profile`/`arc_stages`、场景 | 符合人物声纹的对白 | 不泄露超出 `knowledge_edges` 的信息（信息差硬约束，详见第 4 节） |

设计要点：

- **`PlannerSkill` 的产物是契约不是建议**（硬原则 6）：beat sheet 进 `beats`/`chapter_cards` 表，`ChapterDraftSkill` 的 DoD 直接断言"覆盖全部 beat"——把"追更力"做成一等数据与可验收项，而非 prompt 里的软提示。
- **`ContinuityCheckSkill` 内部就是双流水线**（呼应改动项：continuity_check 拆双流水线）：按 claim 类型路由——确定性 claim（境界、时间线、库存、金手指、信息差）走 Python validators 产 hard issues；模糊 claim（动机、氛围一致）走 LLM-judge 产 soft issues。只有 hard blocker 才阻断放行。
- **`ChapterDraftSkill` 绝不写 bible**（硬原则 2）：它只产 `BibleChangeProposal{op, target_id, old, new, reason, evidence_refs}`，由 Gate 阶段决定去向。Draft 自身只把正文落到 L0（受限 workspace 的草稿区），canon 目录对它只读（7.8）。

---

### 7.5 主生成循环：完整伪代码

主循环是双模式的唯一管线，在 **Gate** 处按 `config.canon_governance.mode` 分叉（硬原则 5）。下面是 Orchestrator 的实现级伪代码。

```python
# control_plane/orchestrator.py
def generate_chapter(project_id: str, n: int, cfg: CanonGovernance) -> ChapterOutcome:
    # ---- 0. 初始化预算账本 + 断路器（见 7.6）----
    budget = BudgetLedger(
        token_cap=cfg.budget_per_chapter.tokens,
        usd_cap=cfg.budget_per_chapter.usd,
    )
    breaker = CircuitBreaker(budget=budget, revise_max_rounds=cfg.revise_max_rounds)
    ws = RestrictedWorkspace(project_id, mode=cfg.mode)   # canon 对自动 Skill 只读
    ctx = SkillContext(project_id=project_id, target_chapter=n, mode=cfg.mode,
                       as_of_chapter=n - 1, budget=budget, workspace=ws,
                       llm=LLMGateway(budget=budget, backoff=ExpBackoff()),
                       repos=open_repos(project_id))

    # ============ 1. PLAN ============
    plan = registry.invoke("PlannerSkill", ctx,
                           card=ctx.repos.chapter_cards.get(n),
                           pacing=ctx.repos.pacing.state(),
                           open_foreshadow=ctx.repos.foreshadow.open_at(n))
    breaker.guard()                                      # 预算/断路检查点
    beat_sheet = plan.outputs["beat_sheet"]             # 章节起草契约

    # ============ 2. RECALL ============
    # as-of 投影：把一致性变成写时约束（硬原则 3）
    world = ctx.repos.world_state.project(as_of_chapter=n - 1)
    # 实体优先的结构化召回（硬原则 4），BM25/向量仅作补充
    recall = Recall(
        entities = ctx.repos.entities.referenced_by(beat_sheet),   # 结构化 SQL，零漏召回
        facts    = ctx.repos.facts.canon_for(world.entity_ids, status="canon"),
        keyword  = ctx.repos.facts_fts.search(beat_sheet.keywords),# FTS5+jieba 补充
        scenes   = ctx.repos.scene_vec.similar(beat_sheet, k=8),   # 可选向量增强，RRF k=60
        taboos   = ctx.repos.constraints.always_on(),              # 否定型禁忌 always-on 硬注入
    )

    # ============ 3. DRAFT ============
    # 稳定前缀（bible 渲染视图/风格/约束）走 1h prompt cache，绝不被章节号/检索结果污染
    draft = registry.invoke("ChapterDraftSkill", ctx,
                            beat_sheet=beat_sheet,
                            world_as_of=world,           # 注入起草 prompt = 写时约束
                            recall=recall.dynamic_part(),# 动态部分单独拼，不进缓存前缀
                            stable_prefix=recall.stable_prefix())
    breaker.guard()

    # ============ 4. CHECK（continuity ‖ craft 并行）============
    cont, craft = parallel(
        lambda: registry.invoke("ContinuityCheckSkill", ctx,
                                draft=draft.outputs["text"], world_as_of=world,
                                claims=extract_claims(draft)),   # 批量校验，非 per-claim fan-out
        lambda: registry.invoke("CraftCheckSkill", ctx,
                                draft=draft.outputs["text"], beat_sheet=beat_sheet),
    )
    breaker.guard()

    # ============ 5. REVISE（≤ revise_max_rounds 轮；revise 后必重跑 check）============
    rounds = 0
    while has_blocker(cont, craft) and rounds < cfg.revise_max_rounds:
        if not breaker.can_continue():                  # 预算/断路：带标记中止修订
            break
        draft = revise(ctx, draft, blockers=collect_blockers(cont, craft))
        # —— 关键：revise 后必须重跑 check，不得跳过 ——
        cont, craft = parallel(
            lambda: registry.invoke("ContinuityCheckSkill", ctx,
                                    draft=draft.outputs["text"], world_as_of=world,
                                    claims=extract_claims(draft)),
            lambda: registry.invoke("CraftCheckSkill", ctx,
                                    draft=draft.outputs["text"], beat_sheet=beat_sheet),
        )
        rounds += 1
        breaker.guard()

    # 放行判据：无 continuity blocker。残留冲突进未决账本带标记继续（见 7.6/7.7）
    unresolved = collect_blockers(cont, craft) if has_blocker(cont, craft) else []
    if unresolved and cfg.continuity_gate == "block" and cfg.mode == "human_gate":
        return ChapterOutcome.held(draft, reason="continuity_blocker", issues=unresolved)

    # ============ 6. GATE（PromotionPolicy 按模式分叉，见 7.7）============
    proposals = draft.outputs["bible_change_proposals"]   # 结构化 fact diff
    state_changes = draft.outputs["state_transitions"]    # 草稿声明的状态迁移
    gate = PromotionPolicy(cfg).decide(
        proposals=proposals, state_changes=state_changes,
        evidence=draft.evidence(), unresolved=unresolved, world_as_of=world,
    )   # → 每个变更得到 route: commit_canon / enqueue_review / hold_staging / reject

    # ============ 7. COMMIT（L0 落盘 + 异步 pipeline）============
    chapter_path = ws.write_draft(n, draft.outputs["text"])   # L0 落文件，表里只存路径
    apply_gate_routes(ctx, gate, chapter_meta={"path": chapter_path, "chapter": n})
    PipelineManager.enqueue(project_id, n,                     # 三档异步触发，详见第 6 节
                            l1="each_chapter", l2="per_3to5_or_volume",
                            l3="canon_event_driven")
    return ChapterOutcome.done(draft, gate=gate, unresolved=unresolved, rounds=rounds)
```

几个不可省略的循环不变量：

1. **Recall 的 as-of 投影是写时约束**：`world = project(as_of=n-1)` 注入 Draft prompt，使一致性从"事后纠错"前移为"写时约束"；Check 再做事后兜底——草稿声明的状态迁移必须能从 `as_of(n-1)` 经合法迁移到达（硬原则 3）。
2. **稳定前缀与动态召回严格分离**（硬原则 10）：`stable_prefix`（bible 渲染视图/风格/约束）进 1h prompt cache；`recall.dynamic_part()`（按章变化的检索结果）单独拼接，绝不污染缓存前缀——后者是头号 silent cache invalidator。
3. **revise 后必重跑 check**：循环结构强制"改完再验"，杜绝"改了但没复验就放行"。
4. **批量校验**：`extract_claims` 一次性抽取整章 claim 交 `ContinuityCheckSkill` 批量判，替代 per-claim fan-out（硬原则 10）。

---

### 7.6 Circuit Breaker 与成本护栏

全自动模式必须有断路器，否则一个失控的修订循环能烧光预算（硬原则 10）。护栏分四类，全部由 Orchestrator 在每个 `breaker.guard()` 检查点强制。

#### 7.6.1 预算账本与断路器

```python
# control_plane/budget.py
class BudgetLedger:
    token_cap: int                       # 每章 token 上限
    usd_cap: float                       # 每章美元上限
    session_token_cap: int = 0           # 每会话上限（0=不限）；跨章累加
    session_usd_cap: float = 0.0
    spent_tokens: int = 0
    spent_usd: float = 0.0

    def charge(self, usage: LLMUsage) -> None:
        # 命中缓存的 token 按缓存价计；用 usage.cache_read_input_tokens 验证命中
        self.spent_tokens += usage.billable_tokens()
        self.spent_usd    += usage.usd()

    def exceeded(self) -> bool:
        return (self.spent_tokens >= self.token_cap or self.spent_usd >= self.usd_cap
                or (self.session_token_cap and self.spent_tokens >= self.session_token_cap)
                or (self.session_usd_cap   and self.spent_usd    >= self.session_usd_cap))

class CircuitBreaker:
    def __init__(self, budget: BudgetLedger, revise_max_rounds: int):
        self.budget = budget
        self.revise_max_rounds = revise_max_rounds
        self.tripped = False

    def can_continue(self) -> bool:
        return not self.budget.exceeded()

    def guard(self) -> None:
        """硬断路：超预算立即抛断路异常，由 Orchestrator 捕获并优雅收尾（落已生成内容 + 标记）。"""
        if self.budget.exceeded():
            self.tripped = True
            raise CircuitTripped(reason="budget_exceeded",
                                 spent_usd=self.budget.spent_usd,
                                 spent_tokens=self.budget.spent_tokens)
```

四类护栏汇总：

| 护栏 | 阈值来源 | 触发动作 |
|---|---|---|
| 每章 token/美元上限 | `config.budget_per_chapter` | 超限 → `CircuitTripped`，落已生成内容并标记未完成 |
| 每会话 token/美元上限 | `config.budget_per_chapter.session_*` | 跨章累加，超限 → 停止后续章节调度 |
| 修订轮数上限 | `config.canon_governance.revise_max_rounds` | 达上限 → 退出 revise 循环，残留冲突进未决账本 |
| 429 / 5xx 退避 | `LLMGateway` 内置 | 指数退避重试，重试耗时计入会话预算 |

#### 7.6.2 LLM 网关：退避与缓存验证

```python
# control_plane/llm_gateway.py
class LLMGateway:
    def __init__(self, budget: BudgetLedger, backoff: ExpBackoff): ...

    def call(self, *, tier: ModelTier, system_stable: str, dynamic: str,
             cache_prefix: bool) -> LLMResponse:
        attempt = 0
        while True:
            try:
                resp = self._client.messages(
                    model=MODEL[tier],
                    system=system_stable,            # 稳定前缀，cache_control: 1h（cache_prefix=True 时）
                    messages=[{"role": "user", "content": dynamic}],
                )
                # 验证缓存命中：cache_read_input_tokens 应 > 0（命中）
                self.budget.charge(LLMUsage.from_anthropic(resp.usage))
                if cache_prefix and resp.usage.cache_read_input_tokens == 0 and attempt == 0:
                    log.warning("prefix cache MISS — 检查前缀是否被章节号/uuid/检索结果污染")
                return resp
            except RateLimitError as e:            # 429
                attempt += 1
                self.backoff.sleep(attempt)        # 指数退避（带 jitter）；耗时计会话预算
            except ServerError as e:               # 5xx
                attempt += 1
                if attempt > self.backoff.max_retries:
                    raise
                self.backoff.sleep(attempt)
```

**注**：以上 Anthropic SDK 用法（`cache_control` 1h prompt cache、`usage.cache_read_input_tokens` 验证缓存命中、429/5xx 重试语义、各档模型 ID 与计价）应在落地时以 Anthropic 官方最新文档为准核对，因 API 细节随版本演进；本节给出的是工程结构，不固化具体字段名/价格。

#### 7.6.3 未决冲突账本：带标记继续写

自动模式下，断路或达修订上限时**不应整章丢弃**，而是把残留冲突记入账本、给章节打标后继续（避免一处冲突卡死整本连载）。

```sql
-- 未决冲突账本（与治理 promotion_log/review_queue 同库；详见第 3 节治理表）
CREATE TABLE IF NOT EXISTS unresolved_conflicts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter       INTEGER NOT NULL,
    issue_code    TEXT NOT NULL,          -- 如 power_rank_skip / timeline_violation
    severity      TEXT NOT NULL,          -- blocker / warn
    detail        TEXT NOT NULL,
    evidence_refs TEXT,                   -- 出处指针（可验）
    raised_by     TEXT NOT NULL,          -- skill_name@version
    status        TEXT NOT NULL DEFAULT 'open',  -- open / acknowledged / resolved
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
```

带标记继续的规则：草稿正文照常落 L0，但**任何未通过硬校验的状态变更一律不晋升**（见 7.7/7.8），因此不会污染后续 Recall 基准；章节在 UI/审计中显示 `has_unresolved_conflicts` 徽标，供作者回看或 `human_gate` 模式下集中处理。

---

### 7.7 Gate 阶段：PromotionPolicy 与草稿层/canon 层分离

Gate 是双模式分叉的唯一决策点（硬原则 5、8）。它读 `config.canon_governance`，对 Draft 产出的每个变更决定**去向**。

#### 7.7.1 草稿层 vs canon 层（核心隔离）

| 层 | 载体 | 谁能写 | 是否进 Recall 基准 |
|---|---|---|---|
| **草稿层** | L0 章节文件 + `fact_candidates`(staging, status=`proposed`) | 自动 Skill 可写 | **否**——`as_of` 投影只读 `status=canon` |
| **canon 层** | `facts` / `fact_revisions` / World State Store 各表 | 仅 `commit_canon`（人审通过或低风险 auto） | 是 |

**这是整个自动模式安全性的基石**：自动模式下章节正文与其声明的状态变更**先全部进草稿层**。只有通过硬校验（无 continuity blocker）且经 PromotionPolicy 放行的状态变更，才晋升到 `state_timeline` / World State 各表（如 `character_power_log`、`item_ownership`、`knowledge_edges`、`timeline_events`）。**未通过的变更停在 `fact_candidates`，绝不进入 canon，因而 7.5 中下一章 Recall 的 `project(as_of=n-1)` 不会读到它们——错误状态不污染后续生成基准**，把潜在的连环漂移在第一章就截断。

#### 7.7.2 PromotionPolicy 决策伪代码

```python
# governance/promotion_policy.py（决策逻辑；表与 API 详见第 3 节）
class Route(str, Enum):
    COMMIT    = "commit_canon"     # 候选 promoted + 新 fact canon（直接落 canon）
    REVIEW    = "enqueue_review"   # 入人审队列（pending_review）
    HOLD      = "hold_staging"     # 留 staging 待证据积累（候选停留 proposed）
    REJECT    = "reject"           # 拒（候选 rejected）

class PromotionPolicy:
    def __init__(self, cfg: CanonGovernance): self.cfg = cfg

    def decide(self, *, proposals, state_changes, evidence,
               unresolved, world_as_of) -> GateDecision:
        decisions = []
        for ch in (proposals + state_changes):
            # —— 步骤 1：所有变更先落 staging（status=proposed）——
            stage_candidate(ch)                      # 写 fact_candidates

            # —— 步骤 2：高风险强制人审（硬原则 5/8）——
            if self._is_high_risk(ch):
                decisions.append(Decision(ch, Route.REVIEW, "high_risk_require_human"))
                continue

            # —— 步骤 3：有未决硬冲突的相关变更不放行 ——
            if self._touched_by_unresolved(ch, unresolved):
                decisions.append(Decision(ch, Route.HOLD, "blocked_by_unresolved"))
                continue

            # —— 步骤 4：按模式分叉 ——
            if self.cfg.mode == "human_gate":
                decisions.append(Decision(ch, Route.REVIEW, "mode_human_gate"))
            elif self.cfg.mode == "auto_promote":
                # 晋升依据 = evidence_strength（可验，权重最高）+ 无冲突 + 非高风险
                if self._evidence_strong(ch, evidence) and not self._conflicts(ch, world_as_of):
                    decisions.append(Decision(ch, Route.COMMIT, "auto_evidence_strong"))
                else:
                    decisions.append(Decision(ch, Route.REVIEW, "auto_low_evidence"))
            else:  # hybrid：低风险软记忆 auto，其余人审
                route = Route.COMMIT if self._is_low_risk_soft(ch) else Route.REVIEW
                decisions.append(Decision(ch, route, "hybrid_split"))
        return GateDecision(decisions)

    def _is_high_risk(self, ch) -> bool:
        # require_human_for：world_rule / power_system / character_death /
        # foreshadow_payoff / knowledge_edges 变更 —— auto 模式下仍强制人审
        return ch.target_type in self.cfg.require_human_for

    def _evidence_strong(self, ch, evidence) -> bool:
        # confidence 不作晋升闸门、只作排序；晋升看 evidence_strength（出处可验）
        return evidence.strength_of(ch) >= self.cfg.auto_promote_threshold
```

执行决策时（`apply_gate_routes`）：

- `Route.HOLD` → 候选停留 `fact_candidates`（status=proposed）待证据积累，写 `promotion_log`（actor=auto, decision=hold_staging）。
- `Route.REVIEW` → 入 `review_queue`（候选 status=pending_review），记 `promotion_log`（decision=enqueue_review, policy_mode）。
- `Route.COMMIT` → 候选 promoted + 写 `facts`/`fact_revisions`（只追加 + 状态变更，新 fact status=canon）与 World State 表，记 `promotion_log`（decision=commit_canon, old/new/reason/evidence_refs）。
- `Route.REJECT` → 候选 `fact_candidates` status=rejected，记 `promotion_log`（decision=reject, reason）。

三条与硬原则的硬绑定：

1. **`require_human_for` 在 auto 模式下仍强制人审**（步骤 2，硬原则 5/8）：境界/世界规则/金手指/角色死亡/伏笔回收/知情者图变更，即便全自动也走 `review_queue`。
2. **confidence 不作闸门、只作排序**（硬原则 8）：自动放行的依据是 `evidence_strength`（出处可程序验证）+ 无冲突 + 非高风险，不是模型自报的置信度。
3. **所有放行/驳回都进 append-only 审计**（硬原则 9）：`promotion_log` 只增不改，记 actor/op/old/new/reason/evidence_refs/policy_mode，支持按 entity 的变更时间线与单条 revert（revert 也是一次新 append）。治理表与 API 详见第 3 节。

---

### 7.8 受限 Workspace

Skill 不能拿到裸文件系统句柄。所有读写经 `RestrictedWorkspace`，提供四层防护：**路径白名单 + realpath 防越界 + 乐观锁 stale check + 审计日志**。

```python
# control_plane/workspace.py
import os
from pathlib import Path

class WriteScopeViolation(Exception): ...
class PathEscape(Exception): ...
class StaleWriteError(Exception): ...

class RestrictedWorkspace:
    def __init__(self, project_id: str, mode: str):
        self.root = Path(self._project_root(project_id)).resolve()
        self.mode = mode
        # 白名单：草稿区可写；canon 区对自动 Skill 只读
        self.read_allow  = [self.root / "drafts", self.root / "canon",
                            self.root / "index"]
        self.write_allow = [self.root / "drafts", self.root / "candidates"]
        # 关键：canon 目录置于自动 Skill 写白名单之外
        #       自动 Skill 只读 canon、只能写 candidate（呼应 7.7 草稿/canon 隔离）

    def _safe(self, rel: str, allow: list[Path]) -> Path:
        # realpath 防越界：解析符号链接与 ../ 后必须仍在白名单子树内
        target = (self.root / rel).resolve()
        if not any(_is_within(target, base) for base in allow):
            raise PathEscape(f"{target} escapes allowed scopes")
        return target

    def read(self, rel: str) -> bytes:
        p = self._safe(rel, self.read_allow)
        self._audit("read", p)
        return p.read_bytes()

    def write_draft(self, chapter: int, text: str) -> str:
        # 自动 Skill 只能写 drafts/candidates；canon 写入由 commit_canon 走治理路径
        p = self._safe(f"drafts/ch{chapter:04d}.md", self.write_allow)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        self._audit("write", p)
        return str(p.relative_to(self.root))   # 表里只存路径（硬原则 11）

    def update_with_lock(self, rel: str, new: bytes, expected_version: int) -> None:
        # 乐观锁 stale check：写前比对版本号，过期则拒绝（防并发覆盖）
        p = self._safe(rel, self.write_allow)
        cur = self._version_of(p)
        if cur != expected_version:
            raise StaleWriteError(f"{rel}: expected v{expected_version}, found v{cur}")
        p.write_bytes(new); self._bump_version(p)
        self._audit("update", p, version=cur + 1)

    def _audit(self, op: str, path: Path, **extra) -> None:
        # 每次读写落审计日志（actor=当前 skill_run, op, path, ts）
        WorkspaceAudit.append(op=op, path=str(path), actor=current_skill_run_id(),
                              ts=now(), **extra)
```

四层防护与硬原则的对应：

- **路径白名单**：`read_allow` / `write_allow` 显式枚举可访问子树；`canon` 在读白名单、不在写白名单——**自动 Skill 只读 canon、只能写 candidate**，从文件系统层面再次落实 7.7 的草稿/canon 隔离与硬原则 5。
- **realpath 防越界**：`_safe()` 先 `resolve()` 再做子树包含判定，挫败 `../`、符号链接逃逸等路径穿越。
- **乐观锁 stale check**：`update_with_lock` 比对版本号，避免并发/异步 pipeline 与人工编辑互相覆盖。
- **审计日志**：每次读写 append 一条记录（actor/op/path/ts），与治理审计（`promotion_log`/`canon_changelog`）共同构成防漂移的 append-only 证据链（硬原则 9）。

canon 真相源始终是 `facts`/`fact_revisions` 表，canon 目录内的文件（如 `story_bible.md`）是从 SQLite 确定性渲染的**只读视图**，不可手改、不被 LLM 写回（硬原则 2、11）——这也是它对自动 Skill 写白名单关闭的另一层理由。L0 草稿存文件、表里只存路径与索引；索引可丢可从 L0/L1 一键重放重建（硬原则 11）。

---

### 7.9 本节与其他节的衔接

- **数据读写形状**（L0/L1/L2、World State 各表的 DDL 与 as-of 投影实现）：详见第 2 节、第 4 节。
- **治理表与 API**（`fact_candidates` / `promotion_log` / `review_queue` / `canon_changelog`、晋升状态机、revert）：详见第 3 节。
- **异步 PipelineManager 三档触发**（L1 每章 / L2 按卷或 3–5 章 / L3 canon 事件驱动）：详见第 6 节。
- **工艺层数据**（`beats` / `chapter_cards` / `character_cards` / `foreshadow` / `pacing_state`）的表结构：详见对应数据层章节；本节只定义生产它们的 Skill 与消费它们的主循环。
