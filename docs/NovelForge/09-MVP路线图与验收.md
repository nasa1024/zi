## 9. MVP路线图与验收标准

> 本节给出 NovelForge 的分阶段交付路线图（MVP0→MVP3），每阶段写清**范围 / DoD（Definition of Done，验收判据） / 风险**，并在末尾给出"开放决策的默认建议"。
>
> 核心排序原则（对原 draft 的纠正）：**最贵、最不稳、最易出错的能力（L1 LLM 抽取、近邻去重、冲突检测、embedding/RRF）必须后置，而不是前置。** 原 draft 把"LLM 抽取 + 去重 + 冲突检测"放在 MVP 第一站，会导致整个系统在最不可靠的环节上构建上层，治理成本与幻觉成本在第一天就爆炸。正确顺序遵循硬原则 7（确定性 validator 是最该早做、最易单测、最易维护的部分）与硬原则 1（硬状态走关系表、软记忆才走 RAG）：
>
> - **先确定性、后概率性**：先把"纯 SQL + 算术 + networkx + 状态机、零 LLM"的硬状态最小集与 validators 做扎实（MVP0），再引入 LLM 抽取（MVP1，且只做"辅助录入"不自动入库），最后才上去重/冲突/embedding（MVP2）与全自动（MVP3）。
> - **抽取从一开始就不是入库**：MVP0 的 facts 表由人工维护；MVP1 引入 LLM 抽取也只是产出候选进 `fact_candidates`(staging) 等人 promote（详见第 3 节晋升闸门）。任何阶段都不存在"LLM 抽取结果直接成为 canon"。
> - **验收用"追更力"而非"设定一致率"**：MVP0 的成败由**真实读者的追更留存**判定（连续试读 5 章后是否愿意看第 6 章），而非一致性指标。一致性是不扣分项、追更力是得分项（硬原则 6），所以早期就必须用读者留存校准方向，避免做出"零矛盾但没人看"的产品。

各阶段能力增量一览（每阶段只在上一阶段基础上**新增**，管线骨架自 MVP0 起即固定为 `Plan→Recall→Draft→Check→Revise→Gate→Commit`）：

| 阶段 | 一句话定位 | 新增核心能力 | 验收锚点 |
|---|---|---|---|
| MVP0 | 确定性骨架 + 人工 canon | L0 落盘 + 人工 facts 表 + World State 硬状态最小集 + FTS5 召回 + 起草前实体召回注入 + 确定性 validators 最小集 | **追更力**：5 章试读留存 |
| MVP1 | LLM 辅助录入 + as-of 投影 + 规划 | LLM 抽取候选进 staging 等人 promote + `get_world_state(as_of)` 投影 + beat sheet 规划 | 录入提速 + 写时一致性注入生效 |
| MVP2 | 去重 + 冲突 + 工艺层 | 近邻去重 + 冲突检测 + embedding/RRF + PacingController/craft_check/voice_profile | 冲突召回率 + 工艺指标可量化 |
| MVP3 | 全自动小说家 | auto_promote + circuit breaker + 多卷/分支 + 冷启动反向抽取 | 无人值守稳定连写 + 成本封顶 |

---

### 9.1 MVP0：确定性骨架 + 人工维护 canon（"零 LLM 抽取"地基）

**目标**：在**不依赖任何 LLM 抽取**的前提下，把一条端到端可跑的连写管线立起来，并用真实读者验证"这书有人追"。本阶段所有"硬状态"由人工维护或确定性程序计算，LLM 只负责正文创作本身（Draft），不负责任何入库决策。

#### 9.1.1 范围（IN）

1. **L0 草稿落盘（数据平面 Memory Core 的 L0 层）**
   - 草稿正文存文件（`drafts/chXXXX.md`），SQLite 表内只存路径与索引（硬原则 11）。
   - 单一 `novel.db`（WAL 模式），业务表 + `facts_fts`(FTS5) 同库同事务；`.backup` 定期快照。
   - 索引可丢可重建：从 L0/L1 一键重放即可重建 FTS5 索引。

2. **人工维护 `facts` / `fact_revisions`（canon 真相源，只追加账本）**
   - 提供最小录入 API/CLI：`add_fact`、`revise_fact`（只追加 + 状态变更 canon/tentative/retconned，绑定 `valid_from_chapter`，永不物理删除，详见第 2 节）。
   - `story_bible.md` 从表确定性渲染为**只读视图**（不可手改、不被 LLM 写回）。

3. **World State 硬状态最小集（World State Store，人工录入 + 确定性计算）**——只做以下五项，**不做语义分层**：
   - **entities 名字规范化**：`entities(canonical_name, aliases)`，别名→canonical 的确定性归一。
   - **power_ranks 境界单调性**：有序枚举词表 + `character_power_log`，确定性校验"境界不可无标注下降"。
   - **timeline 绝对时间线 + 移动耗时**：`timeline_events(绝对 story_time)` + `geo_locations` + `travel_edges`，确定性校验时序与"两地之间移动耗时 ≥ 最短路"。
   - **foreshadow 到期扫描**：`foreshadow(planted→…→overdue)` 的到期扫描（按当前章节号扫 overdue，详见第 4 节）。
   - **item 库存**：`item_ownership` + `item_log`，确定性的"道具/金手指库存守恒"。

4. **FTS5 + 起草前实体召回注入（Recall，结构化主路）**
   - 召回主路 = 按 entity / 章节范围 / status 的结构化 SQL（零漏召回、可解释，硬原则 4）。
   - `facts_fts`(FTS5 + jieba 预分词) 作关键词补充。
   - **起草前把召回到的相关 facts/实体硬注入 Draft prompt**（写时约束雏形）。
   - 否定型/全局禁忌 always-on 硬注入 system，不走检索。

5. **确定性 validators 最小集（治理平面 Check 阶段的 hard issues 流，纯 SQL + 算术 + networkx + 状态机、零 LLM）**
   - 对应上面五类硬状态各一个 validator：名字规范化、境界单调性、绝对时间线 + 移动耗时、伏笔到期扫描、道具/金手指库存。
   - Check 阶段仅跑 `continuity_check` 的确定性子流水线，产出 hard issues（craft_check 推迟到 MVP2）。
   - `continuity_gate` 默认 `warn`（人审为主，不强制 block），详见第 8 节配置根。

6. **管线骨架（控制平面 Orchestrator）**
   - 跑通 `Plan(人工/极简)→Recall→Draft(LLM 正文)→Check(仅确定性 continuity)→Revise(人工)→Gate(全部 human_gate)→Commit(人工 promote)`。
   - 缓存纪律从第一天落地：稳定前缀（bible/风格/约束）走 1h prompt cache，**绝不被章节号/时间戳/uuid/每次变化的检索结果污染**（硬原则 10，头号 silent invalidator）。

#### 9.1.2 范围外（OUT，明确不做）

- 不做 LLM 抽取入库（facts 全人工）；不做 staging 自动流（MVP1）。
- 不做近邻去重、冲突检测、embedding/RRF、`scene_vec`（MVP2）。
- 不做 as-of 投影 `get_world_state`（MVP1）、beat sheet 自动规划（MVP1）。
- 不做 craft_check / PacingController / voice_profile（MVP2）。
- 不做 auto_promote / circuit breaker / 多卷分支 / 冷启动（MVP3）。

#### 9.1.3 DoD（验收判据 —— 用"追更力"而非"设定一致率"）

**主判据（成败由它决定）**：
- **真实读者追更留存**：招募 ≥ 5 名目标品类真实读者，连续试读**前 5 章**，统计"读完第 5 章后愿意看第 6 章"的比例。
  - **通过线（默认建议，见 9.5）：留存 ≥ 60%（5 人中 ≥ 3 人）愿看第 6 章**。
  - 未达线 → 不进入 MVP1，回到正文创作/选题/爽点契约迭代（一致性引擎再完备也不救场）。

**辅助判据（工程就绪，必须全绿但不单独决定成败）**：
- 端到端能在一条命令内从 Plan 跑到 Commit 产出一章并落盘 L0。
- 五个确定性 validator 各有单元测试覆盖（构造正例 + 反例，断言 hard issue 命中/不命中）。
- `story_bible.md` 完全由 `facts` 渲染，手动改表 → 重渲染 → diff 一致；人工改 md 不影响真相源。
- prompt cache 命中可验证：连续两章生成，第二章 `usage.cache_read_input_tokens > 0` 且稳定前缀未失效。
- `novel.db` 删除 FTS5 索引后可从 L0/facts 一键重建，重建后召回结果一致。

#### 9.1.4 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 人工维护 facts 表负担过重，作者弃用 | 地基没人填，后续阶段无数据 | 录入字段最小化；MVP1 用 LLM 抽取做"辅助录入"减负（候选预填，人只 promote） |
| 试读样本小、留存判据噪声大 | 误判方向 | 固定品类/固定开篇钩子模板；样本不足时用同一批读者多书对照 |
| prompt cache 被检索结果/章节号污染 | 成本失控、命中率为 0 | 把"每次变化的内容"严格隔离在 cache 断点之后；CI 断言 cache_read > 0 |
| validator 误判过严打断创作 | 作者反感 | `continuity_gate=warn`，hard issue 只提示不阻断，MVP0 不上 block |

---

### 9.2 MVP1：LLM 辅助录入 + as-of 投影 + beat sheet 规划

**目标**：在确定性地基稳固后，用 LLM 把"人工录入 facts"升级为"**LLM 抽取候选 → 进 staging → 等人 promote**"的辅助录入（**绝非自动入库**），并引入 as-of 投影把一致性从事后兜底升级为**写时约束**，同时引入逐章 beat sheet 让 Draft 有契约可依。

#### 9.2.1 范围（IN）

1. **LLM 抽取候选 → `fact_candidates`(staging)（辅助录入，非自动入库）**
   - ChapterDraft 提交后异步触发 L1 抽取（PipelineManager L1 每章异步触发，详见第 6 节）。
   - LLM 只产出结构化 `BibleChangeProposal{op, target_id, old, new, reason, evidence_refs}` 与 `fact_candidates`（状态 `proposed`），**绝不写回 bible**（硬原则 2）。
   - 结构化抽取用 instructor + Pydantic `field_validator` 自愈重试（硬原则 11）。
   - 抽取用 Haiku/Sonnet，不动 Opus（硬原则 10）。

2. **单一晋升闸门 + review_queue（治理平面，本阶段全部走 human_gate）**
   - 唯一 `PromotionPolicy` 决策点：本阶段 `mode=human_gate`，`enqueue_review` 把候选送 `review_queue`，人审后 `commit_canon`。
   - `promotion_log` append-only 起用（记 actor/op/old/new/reason/evidence_refs/policy_mode，硬原则 9，唯一权威 DDL 见第 2 节）；内容变更流由 `fact_revisions` 承担；提供按 entity 的变更时间线与单条 revert（revert 也是一次新 append）。

3. **as-of 投影 `get_world_state(as_of_chapter=N)`（World State Store）**
   - 把一致性升级为**双重保障**（硬原则 3）：起草前把 as-of(N) 注入 Draft prompt（写时约束）；Check 阶段校验"草稿状态必须从 as-of(N) 经合法迁移到达"（事后兜底）。

4. **beat sheet 规划（PlannerSkill → ChapterDraftSkill 契约，控制平面 Skill Registry）**
   - `PlannerSkill` 产出逐章 beat sheet（写入 `beats` / `chapter_cards`），作为 `ChapterDraftSkill` 的输入契约（硬原则 6）。
   - 日更粒度与"核心爽点契约"作为 GenerationContract 输入（详见第 5 节与 9.5 默认建议）。

#### 9.2.2 范围外（OUT）

- 仍不做 auto_promote（全部 human_gate，require_human_for 此阶段实际是全集）。
- 仍不做去重 / 冲突检测 / embedding / craft_check（MVP2）。
- 仍不做多卷分支 / 冷启动（MVP3）。

#### 9.2.3 DoD

- **录入提速可量化**：同一章，"人工从零录 facts"对比"LLM 候选预填 + 人 promote"，**人均录入操作步数/时长下降 ≥ 50%**，且人审最终入库内容质量不低于纯人工。
- **写时一致性注入生效**：注入 as-of(N) 后，确定性 validator 在新章上的 hard issue 数较 MVP0（无写时注入）**显著下降**（对照同批章节统计）。
- **抽取不自动入库可证**：审计中不存在 `actor=llm` 且 `op=commit_canon` 的记录；所有 canon 变更的 `policy_mode=human_gate` 且有人 actor。
- **append-only + revert 可用**：对任一 entity 可拉出变更时间线；执行单条 revert 后真相源回到目标态，且 revert 本身作为新 append 出现在 `promotion_log`。
- **beat sheet 契约生效**：Draft 输入包含该章 beats；可统计"Draft 实际覆盖的 beat 比例"。

#### 9.2.4 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| LLM 抽取幻觉/漏抽 | 候选噪声，人审负担反增 | instructor + Pydantic 校验自愈；evidence_refs 出处必填，无出处候选低排序 |
| 人审队列积压 | 连写节奏被治理拖慢 | 高风险才进队列（为 MVP3 的分级铺路）；候选按 evidence_strength 排序优先处理 |
| as-of 投影实现复杂、性能差 | 起草延迟 | 投影只覆盖五类硬状态；networkx 仅查询期内存 build、跑完即弃（硬原则 11） |
| 缓存被 as-of 注入内容污染 | 命中率下降 | as-of 结果放 cache 断点之后；稳定前缀仍只含 bible/风格/约束 |

---

### 9.3 MVP2：近邻去重 + 冲突检测 + embedding/RRF + 完整网文工艺层

**目标**：在治理闭环稳定后，才引入**最不稳定**的概率性能力（去重、冲突检测、embedding 相似桥段），并把"追更力"从经验判断升级为可量化的**网文工艺层**（一致性与工艺并行，硬原则 6）。

#### 9.3.1 范围（IN）

1. **近邻去重（候选入 staging 前的合并）**
   - 新候选与既有 facts/候选做近邻匹配，避免同一事实重复入库；命中近邻 → 转为 `update`/合并提案而非新增。

2. **冲突检测（Check 阶段双流水线的 soft issues 流补全）**
   - `continuity_check` 拆为双流水线（详见第 4 节）：确定性 validators 产 hard issues（MVP0 已有）‖ LLM-judge 产 soft issues（本阶段补全），按 claim 类型路由。
   - confidence 不作晋升闸门、只作排序（硬原则 8）。

3. **embedding / RRF + `scene_vec`（仅作"查相似桥段"的可选增强）**
   - sqlite-vec(vec0) 仅对 L2 场景块建索引（`scene_vec`，硬原则 11）。
   - RRF：k=60、客户端、只用排名不归一化（硬原则 4）；定位为可选增强，结构化 SQL 仍是召回主路。

4. **完整网文工艺层（治理平面之外的独立一等公民）**
   - `PacingController` + `pacing_state(tension_curve)`：张力曲线监控与节奏建议。
   - `craft_check`：Check 阶段与 `continuity_check` **并行**，对 hook / value_shift / payoff_beat / tension_point 打分。
   - `voice_profile`（写入 `character_cards`）：人物声音一致性。

#### 9.3.2 DoD

- **冲突召回可量化**：构造已知冲突测试集（境界越级、时间线矛盾、库存不守恒、知情者图矛盾等），hard issues 召回率 = 100%（确定性必中），soft issues 在文风/动机类冲突上达到设定召回基线。
- **去重有效**：注入重复事实，去重后 `facts` 无重复条目、转为 update 合并，审计可追溯。
- **embedding 为增强非主路**：关闭 `scene_vec` 后召回主路（结构化 SQL + FTS5）仍可用；RRF 开关化、k=60、只用排名。
- **工艺指标可量化**：每章产出 craft_check 评分（hook/value_shift/tension），`pacing_state` 张力曲线可视化；craft_check 与 continuity_check 在 Check 阶段并行执行不互相阻塞。

#### 9.3.3 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| LLM-judge soft issues 噪声高 | 误报淹没真问题 | 批量校验替代 per-claim fan-out（硬原则 10）；soft issues 默认 warn 不 block |
| embedding 召回不相关桥段 | 注入污染、成本上升 | 严格限定"查相似桥段"用途、仅 L2；RRF 只用排名；可一键关闭 |
| 工艺层与一致性层耦合 | 改一处坏一处 | 二者正交、并行流水线、独立数据表（beats/pacing_state vs World State） |
| 去重误合并不同事实 | canon 失真 | 去重只产合并"提案"，仍走 human_gate 人审确认 |

---

### 9.4 MVP3：全自动模式 + 多卷/分支 + 冷启动

**目标**：在前三阶段全部稳定后，才开启**全自动小说家**模式（同一条管线在晋升闸门处分叉到 auto_promote，硬原则 5），并支持长篇规模化（多卷/分支）与存量稿冷启动。

#### 9.4.1 范围（IN）

1. **全自动模式 auto_promote + circuit breaker（控制平面 + 治理平面）**
   - `PromotionPolicy` 在 `mode=auto_promote`/`hybrid` 下，低风险软记忆 auto commit；**`require_human_for`（world_rule / power_system / character_death / foreshadow_payoff / knowledge_edge_change，即知情者图变更）在 auto 模式下仍强制人审**（硬原则 5/8）。
   - 晋升依据 = evidence_strength（出处可验，权重最高）+ 无冲突 + 非高风险；confidence 只排序不晋升。
   - **circuit breaker（硬原则 10，全自动生死线）**：token/美元上限 + 修订轮数上限（`revise_max_rounds`）+ 自动降级条件（见 9.5）；触发即降级为 human_gate 或停机。

2. **多卷 / 分支**
   - 多卷的 World State 续接与卷级 L2 重算（PipelineManager L2 按卷或 3-5 章触发）；支线/分支剧情的 as-of 投影隔离。

3. **冷启动：从存量正文反向抽取初版 canon**
   - 对已有存量正文批量反向抽取，生成 `fact_candidates` 初版 canon（仍进 staging，按规模决定批量人审或抽检 promote）。

#### 9.4.2 DoD

- **无人值守稳定连写**：在 `auto_promote` 下连续生成 ≥ 10 章无需人工干预，且 require_human_for 类变更 100% 被拦下进 review_queue（绝不 auto commit）。
- **成本封顶可证**：单章/整轮触达 token/美元上限时 circuit breaker 必触发并降级，审计可见降级事件与 `policy_mode` 切换。
- **自动降级有效**：注入连续 N 章抽检失败（见 9.5 阈值），系统自动从 auto_promote 降级为 human_gate。
- **冷启动可用**：对一部存量稿反向抽取出初版 `fact_candidates`，人审/抽检后形成可用 canon，后续新章可在其上写时注入并通过确定性 validators。
- **多卷续接**：跨卷生成时 World State 正确续接，跨卷一致性 hard issues 不回升。

#### 9.4.3 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 全自动漂移累积 | 越写越偏、矛盾滚雪球 | append-only 审计 + 单条 revert（硬原则 9）；高风险强制人审；定期抽检 |
| 成本失控 | 连写经济性崩溃 | circuit breaker 硬封顶；Opus 只留正文与冲突复核；`usage.cache_read_input_tokens` 验证命中 |
| 冷启动抽取出错误 canon | 污染后续全部生成 | 冷启动结果一律进 staging 不直接 canon；大规模时按风险分层抽检 |
| 多卷分支状态串台 | as-of 投影错乱 | 分支级 World State 隔离；投影带分支维度 |

---

### 9.5 开放决策的默认建议

> 以下为当前待定项的**推荐默认值**，可在 `config.canon_governance` 等处覆盖。给默认值是为了让团队"先能跑、再调参"，每项附简短理由。

1. **`require_human_for` 默认清单**
   - **默认**：`[world_rule, power_system, character_death, foreshadow_payoff, knowledge_edge_change]`（即世界规则、境界/力量体系、角色死亡、伏笔回收、知情者图变更）。
   - **理由**：这五类都直接触及 World State 的硬一致性，错一处会污染后续全部生成，且不可由相似度挽救（硬原则 5/8）；在 auto_promote 下仍强制人审。

2. **embedding 是否进 MVP / API 还是本地**
   - **默认**：**不进 MVP0/MVP1，推迟到 MVP2**，且**仅作"查相似桥段"的可选增强**（结构化 SQL + FTS5 永远是主路）。**优先本地 embedding 模型**（本地优先原则、零外部依赖、成本可控、隐私），仅在本地效果不足时才切 API。索引仅 `scene_vec`(sqlite-vec on L2)。
   - **理由**：embedding 是最不稳、最易污染召回的能力，必须后置；本地化契合"本地优先 + 单 .db 文件"。

3. **境界体系配置形态**
   - **默认**：**YAML 有序词表**（power_ranks 为有序枚举，序号即偏序），配套"**合法下降事件标注**"机制——境界单调性 validator 默认禁止下降，除非该下降被显式标注为合法事件（如自废修为、封印、伤重跌境），标注写入 `character_power_log` 并被 validator 放行。
   - **理由**：YAML 有序词表对单人维护最直观、可版本化；合法下降标注覆盖网文常见跌境桥段，避免 validator 误杀（硬原则 7）。

4. **全自动模式抽检阈值 N 与自动降级条件**
   - **默认 N = 10**：每自动生成 **10 章**做一次人工抽检（抽检 1-2 章 + 跑全量确定性 validators）。
   - **自动降级条件（满足任一即从 auto_promote 降级为 human_gate）**：
     - 连续 **2 次抽检**出现 hard issue；或单次抽检 hard issue 数 ≥ 阈值；
     - 触达 circuit breaker 的 token/美元上限或 `revise_max_rounds`；
     - 单章修订轮数达到 `revise_max_rounds` 仍未消除 hard issue。
   - **理由**：N=10 兼顾"少打断"与"可控漂移"；降级条件以确定性 hard issue 为准（可程序判定，不依赖 LLM 自觉，硬原则 9/10）。

5. **MVP0 成功判据**
   - **默认**：**追更力优先**——5 名真实读者连续试读前 5 章，**≥ 60%（≥ 3/5）愿看第 6 章**即通过；工程辅助判据（端到端跑通、五 validator 单测、bible 只读渲染一致、cache 命中可验、索引可重建）必须全绿但不单独决定成败。
   - **理由**：一致性是不扣分项、追更力是得分项（硬原则 6）；早期就用真实读者校准方向，避免做出"零矛盾但没人追"的产品。

6. **日更粒度与核心爽点契约作为 GenerationContract 输入**
   - **默认**：**日更粒度 = 单章（约 2000-3000 字/章），每章必须携带一个"核心爽点契约"**（至少一个 value_shift 或 payoff_beat/hook），作为 GenerationContract 的必填输入交给 ChapterDraftSkill（详见第 5 节工艺层）。
   - **理由**：连载的追更力来自"每章一个钩子/爽点"；把它作为契约硬输入，使工艺层（MVP2 的 craft_check）有明确校验对象。

7. **历史存量稿冷启动是否纳入**
   - **默认**：**纳入，但置于 MVP3**，且冷启动反向抽取结果**一律进 staging（`fact_candidates`），绝不直接 commit 为 canon**；大规模时按风险分层抽检 promote。
   - **理由**：冷启动价值大（让存量作者一键迁移），但抽取最不稳，必须在去重/冲突/治理闭环（MVP2）成熟后才做，且严守"抽取不自动入库"的全局红线。
