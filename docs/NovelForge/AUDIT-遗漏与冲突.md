# NovelForge 设计审计 · 遗漏与逻辑冲突报告

> 对 15 节设计文档 + 4 篇实现规格的系统性审计（两轮、12 个审计视角 + 综合者逐行核验去假阳性）。
> 结论：**顶层骨架自洽**（三平面 / 单一 SQLite 真相源 / 确定性 validator+LLM-judge 双流水线 / 双模式闸门分叉 / 草稿-canon 隔离 / MVP 分期），但跨多轮并行撰写累积了**集成债**——主要不是"想错了"，而是"多节对同一对象给了不一致的落地"。所有条目均经原文核验（标 where）。

## 根因聚类（30 条发现归为 7 个主题）

| 主题 | 本质 | 严重度 |
|---|---|---|
| A 真相落地链断裂 | canon/Draft 产物→World State `*_log` 的"应用器"从未定义，commit_canon 只写 facts 不写 `*_log` | 🔴 地基 |
| B Schema/命名漂移 | §04/§05/§06 大面积用 §02 权威 DDL 不存在的列；§10 缺收敛裁定 | 🔴 系统性 |
| C 治理装配错位 | `PromotionPolicy.decide()` 双签名、异步候选无 Gate、字段名分叉、ExtractSkill 归属 | 🔴 核心 |
| D 一致性引擎落地 | MVP0 确定性 claim 抽取器缺/矛盾、误报豁免录入流程缺 | 🟠 |
| E 运行时契约真空 | autopilot/SSE续传/取消/转译层/会话预算累加/结构化与工具共存 | 🟠 |
| F 运维与冷启动 | schema 迁移、备份恢复(含 l0)、崩溃续跑、bible_seed 格式、词典重建、前缀 bounding | 🟠 |
| G 安全与全局禁忌 | prompt injection 边界、REST 鉴权/actor 可伪造、always-on 禁忌体系三处互斥 | 🟠 |

---

## A · 真相落地链断裂（最深）

| # | 类型 | 严重度 | where | 问题 | 修法 |
|---|---|---|---|---|---|
| A1 | 遗漏 | **blocker** | §03.3 commit_canon line185 占位；§07.5 line318 apply_gate_routes 无体；§11.7 commit_canon line475-505 不 INSERT 任何 `*_log` | `fact`/`StateTransition` → World State `*_log`（境界/知情者图/库存/数值/时间线）的投影/应用器**全文从未定义**。as-of 投影从 MVP1 起读到空表，整条一致性链在地基断开 | 新增《投影映射 + apply_gate_routes/apply_state_transition》节；commit_canon 同事务既写 facts 又写对应 `*_log`；给 `facet→表`、`from/to/kind→必填列`（rank_name→rank_id/rank_order 查表等）确定性映射 |
| A2 | 冲突 | high | §07.7.2 line475 把 StateTransition 当候选 stage vs impl/00 §B.3（无 candidate_id/op/risk）vs §02.4.1 promotion_log.candidate_id NOT NULL | StateTransition 落不进 promotion_log、`commit_canon(cand)` 吃不了 | 二选一：转 fact_candidate（补字段）或独立 transition_log + promotion_log 可空 transition_id |
| A3 | 冲突 | high | §03.7.1（旧置 retconned + append 新）vs §11.7 line492-497（就地 UPDATE target 改 canon） | retcon 物理实现互斥，§11.7 违反"只追加"铁律，retcon 后投影读错 | 以 §03.7.1 为准修 §11.7：retcon=mark_retconned(旧)+append(新) |
| A4 | 遗漏 | medium | §03.7.1（只改 facts.status）；§04.4 replay 不 join facts.status；§03.7.3 reproject_affected 仅引用无定义 | retcon/revert 后下游 `*_log` 行不反做（无级联）→ 投影与 canon 漂移 | `*_log` 增 source_fact_id + replay join status 过滤；定义 reproject_affected 全量重放重建 |

## B · Schema / 命名漂移（一次 R12 收敛即可批量修）

| # | 类型 | 严重度 | where | 问题 |
|---|---|---|---|---|
| B1 | 冲突 | **blocker** | §04.* validator SQL vs §02.5.* | World State `*_log` 列名系统性漂移：`character_power_log`(entity/chapter/rank_label→entity_id/change_chapter/rank_id+rank_order)、`knowledge_edges`(knower→knower_entity_id)、`item_log`(owner/item/qty/op→item_entity_id/from/to_owner_id/quantity_delta/change_type)、`timeline_events`(story_time/abs_lo→story_time_start/end)、`travel_edges`(min_hours→travel_cost)、gimmick(fsm_json/cost_schema→activation_cond/cost_json) |
| B2 | 冲突 | **blocker** | §05.6 自有 DDL vs §02.6.* | beats/chapter_cards/character_cards/pacing_state 同名两套不兼容 schema（主键名、章列名、value 取值、整堆列全不同）。§00 第9条已声明收敛意图但 §05.6 未改写 |
| B3 | 冲突 | high | §02.6.4(state/due_chapter) vs §05/§04/§06(status/deadline_chapter/abandoned/overdue_chapter) | foreshadow 状态列名/到期列/值域三处冲突 |
| B4 | 冲突 | high | §02.6.5(逐章快照) vs §05.6(单行累积态 buildup/recent_high_streak/…) | pacing_state 模型互斥，PacingController 决策读的字段权威表全无 |
| B5 | 冲突 | high | §11.7 line467-469 游离 ALTER facts ADD version vs §02.2.1 facts 无 version 列 | 照 §02 建库则 §11.7 乐观锁 SQL 报 no such column |
| B6 | 冲突 | medium | §05.6 beats.value_axis vs §02.6.1 beats 无此列 | 纸片人检测 FLAT_CHARACTER 读不到 value_axis |
| B7 | 冲突 | medium | §02.5.2(多体系 system_name) vs §04/§11(扁平全局 rank_order_map) | 多体系境界序列重叠时 validator 误判越级 |

> **统一修法**：§10 增 **R12**，以 §02 为 DDL 唯一权威逐表钉死列名/表结构（含 version 列正式补进 §02、§05 工艺四表扩列后删除 §05.6 自有 DDL 改为引用、foreshadow 用 state/due_chapter、value_axis 补列、power 校验按 system_name 分组），据此改写 §04/§05/§06 全部 SQL。

## C · 治理装配错位

| # | 类型 | 严重度 | where | 问题 | 修法 |
|---|---|---|---|---|---|
| C1 | 冲突 | **blocker** | §03.3 decide(cand)->Route 纯函数 vs §07.7.2 decide(*,proposals,state_changes,…)->GateDecision 带 staging 写 | `PromotionPolicy.decide()` 两套不兼容签名却都自称"唯一决策点" | §10 钦定唯一签名：§03 纯函数 decide(cand,world,config)->Route 为可单测内核；§07 批量编排改名 decide_batch/GatePlanner 循环调内核 |
| C2 | 遗漏 | **blocker** | §07.5 step6 只裁 draft 产物，step7 enqueue 后 return；§06.2.3 L3 "推给晋升闸门" | 异步 PipelineManager 产的 L1/L3 候选**无人调 PromotionPolicy 再裁决**→治理真空（候选永停 proposed）或未文档化的第二 Gate | 定义 worker 完成抽取后回调 PromotionPolicy.decide() 的入口（含 actor/policy_mode/run_id/chapter）；或明确异步产物只作软记忆、canon 级提案由同步 Draft→Gate 独占 |
| C3 | 冲突 | high | §07.7.2 line506 ch.target_type vs §03/§11 cand.fact_type；两类产物都无 target_type | 高风险判定读到 None → require_human_for 漏判 → auto 误自动晋升（破坏 HP5/8） | 统一 fact_type；StateTransition 用 facet→require_human_for 映射 |
| C4 | 冲突 | high | §01.4/§01.5 时序图(独立 ExtractSkill 在 Check→Gate) vs §07.5/§12.8(ChapterDraftSkill 在 Draft 内直出 proposal) | BibleChangeProposal 生产者/定型时点互斥，影响 Check 能否校验 proposal | 以 §07/§12 为准（Draft 直出），修 §01 时序图删独立 Extract |
| C5 | 遗漏 | medium | §07.4 Skill 清单 / §07.2.1 SkillTrigger / 主循环 均无 ExtractSkill；§01 却列为一等 Skill | ExtractSkill 管线归属未定义 | §07.4 补契约行或声明 extract 由 §06 PipelineManager 内部承担 |

## D · 一致性引擎落地

| # | 类型 | 严重度 | where | 问题 | 修法 |
|---|---|---|---|---|---|
| D1 | 冲突+遗漏 | **blocker** | §04.1(LLM 抽取) vs impl/00 §B.2 + impl/12(确定性正则+词表)；§09 MVP0"零 LLM 抽取" | extract_claims 抽取方式自相矛盾；**MVP0 确定性 claim 抽取器从未设计**→MVP0 Check 空转 | 裁定 MVP0 确定性版 + MVP1 LLM 版关系；复用 §06.3.3 锚点扫描补确定性 extract_claims，钉为五 validator 前置依赖 |
| D2 | 遗漏 | high | §04.5 定义 consistency_exemptions 表 + apply_whitelist，但 §03/§13 无录入流程 | 误报豁免**有表无门**→作者命中误报只能改稿或关引擎（正是 §04.5 要防的） | review_queue 审校界面加"标记为有意(豁免)"动作写 exemptions；给 REST/CLI 入口；到期失效扫描挂每章 Check |

## E · 运行时契约真空（§13/§14/§08）

| # | 类型 | 严重度 | where | 问题 | 修法 |
|---|---|---|---|---|---|
| E1 | 遗漏 | high | §13 反复引用 POST /autopilot/start、GET /status、POST /degrade；§08/§10 grep 命中 0 | 模式2 核心运行时入口三端点**无任何 schema**（参数/返回/状态机/跨章预算归属） | §08 补三端点 Pydantic + §10 登记；定义连写循环/抽检/降级状态机与持久化 |
| E2 | 遗漏 | high | §13.2.4 声明 Last-Event-ID+seq 续传；全文无事件持久化表（turns 仅 result_json 终态） | SSE 断线续传**不可实现**（增量事件发出即丢） | 新增 append-only 事件表(turn_id,seq,type,data,ts) 按 seq 重放；或显式降级声明不支持续传 |
| E3 | 遗漏 | high | §14.7 称 Orchestrator 转译；impl/14 StreamAssembler 只装回 Response；impl/12 ToolLoop 全程用 gw.generate 非 stream | ProviderStreamEvent→业务 SSE 转译层无定义；ToolLoop 用 generate 致 **draft-token 无来源** | 定义转译层模块 + 逐类型映射；Draft 阶段走 gw.stream() |
| E4 | 冲突 | high | §07.5 每章 new BudgetLedger（不传 session cap、spent 从0）vs §07.6.1/§13 "会话级跨章累加" | session 预算无法累加 → **autopilot 会话级熔断失效**（防失控烧钱） | generate_chapter 持跨章 session 账本或从 sessions 行载入/写回 budget_spent |
| E5 | 冲突 | medium | impl/14 §14.10(response_schema→tool_choice 钉死 emit_structured) vs impl/12 ToolLoop(不传 schema、宽松解析) | 结构化输出与工具调用**同一次 generate 不能共存**；解析失败**静默吞 state_transitions**→无声漏晋升 | 终局步走 generate_structured 强约束+修复重试；定义解析失败告警/重试，绝不静默吞 |
| E6 | 冲突 | medium | impl/14 degraded→resp.raw[_degraded] vs impl/12 write_tool_call_log 不读它；tool_call_log 无 degraded 列 | 跨供应商 fallback 审计闭环断裂；stream 不回退/非流式回退韧性不对称 | tool_call_log 增 degraded/fallback 列，ToolLoop 从 Response 读降级原因；声明 stream 回退取舍 |
| E7 | 遗漏 | medium | turns.status 含 'canceled' 但无 cancel 端点/取消传播 | 长任务/连写**无法中途取消**（只能断 SSE 任务仍后台烧钱） | 加 cancel 端点写协作式取消信号，Orchestrator 各 guard 点/ToolLoop 每步检查 |
| E8 | 遗漏 | medium | sessions 只有创建与 /session/end，无 TTL/清理 | 客户端崩溃后 session 永不结束、running turn 永挂 → session 预算无法收口 | 加 heartbeat/last_seen + 清理任务 + 启动恢复钩子 |

## F · 运维与冷启动

| # | 类型 | 严重度 | where | 问题 | 修法 |
|---|---|---|---|---|---|
| F1 | 遗漏 | high | 有 meta_kv.schema_version='2' 但 init_db 只跑全量 DDL；§11.7 又要 ALTER | **schema 升级无迁移机制**：对已写数百章的库做任何变更无可操作路径 | db/migrations/ + migrate(conn) 运行器：按版本号单事务逐版应用并 bump；启动期门控 |
| F2 | 遗漏 | high | §13.4 nf seed --file bible_seed.yaml；但格式/字段 schema 全文未定义 | **冷启动种子文件格式从未定义**→MVP0 人工录入载体缺失，第一份世界观无从写 | §13/§02 附录给 bible_seed.yaml 权威 schema + 示例 + seed→BibleChangeProposal 映射 |
| F3 | 遗漏 | high | §08.5 backup() 只 conn.backup；L0 草稿存 l0/ 文件、是索引重建源 | 备份**只覆盖 novel.db 不含 l0/**，恢复后 draft_index 指向不存在文件；无 restore 手册 | 一致性快照单元=db+l0/+config；给 restore 运行手册（停写→恢复→sha256 校验→rebuild 索引） |
| F4 | 遗漏 | high | §06.5/§06.6 称冷热分离/前缀 bounding；§02 facts 无 volume 列、无冷存储表 | story_bible 稳定前缀随连载增长的 **bounding 只有口号无机制**（无分卷、无归档表、无触发） | §02 增 facts.volume_no/卷边界表；§06 落成可执行：归档判定谓词+触发点+超限兜底 |
| F5 | 遗漏 | high | §02.8.1 称"词典变更⇒重建"；专名词典从 entities/aliases/power_ranks 派生且连载持续增长 | 中文 FTS5 jieba **词典重建触发机制缺失**（谁监测/同步异步/节流） | 增量 add_word 即时止血 + 阈值/卷边界后台全量重建 + 重建期旧索引服务原子切换 |
| F6 | 遗漏 | medium | §07.5 一次 /pipeline/run 跨多个有副作用阶段；无按 run_id 检测半成品续跑 | **崩溃无续跑/幂等恢复**（Idempotency-Key 只防重复提交不防 in-flight 崩溃）→重跑产重复 L0/候选/扣预算 | pipeline_run 状态机表 + 启动期扫描续跑/补偿回滚 |
| F7 | 遗漏 | medium | §06 capture() 先 write_text 再独立事务写 drafts；§02.7.1 有 sha256 无人校验 | L0 文件+db **双写无原子性**→崩溃产孤儿文件/悬空指针，无启动期对账 | temp→fsync→rename + status 两阶段 + 启动 orphan-sweep 按 sha256 对账 |
| F8 | 遗漏 | medium | §07.5 step7 enqueue 与 §06.1 capture 都在 Commit 入队 L1 | Commit 两条 enqueue 并存，归属/去重未定义→同章 L1 可能重复入队 | 统一 Commit 落盘+入队为单一函数 |

## G · 安全与全局禁忌体系

| # | 类型 | 严重度 | where | 问题 | 修法 |
|---|---|---|---|---|---|
| G1 | 遗漏 | high | 全文 grep injection/越狱/sanitize 零命中；注入点：§03.8 bible 渲染进稳定 system、§13.6.2 用户 utterance 经 LLM、§09.4.1 冷启动正文喂抽取 | 进 LLM 提示词的**非可信文本无 prompt injection 防护**（污染 canon/起草 system，不在 validator 覆盖内） | 非系统文本加分隔符+"数据非指令"包裹；parse_intent 强制闭枚举 schema；抽取产物自由文本回灌前转义 |
| G2 | 遗漏 | high | §08.1 基址无 auth；actor 客户端自报且服务端不校验；§13.1 称被其他 Agent 调用 | 本地 **REST API 无鉴权**（任何本地进程能 approve 高风险/revert canon/启动 autopilot）；**actor 可伪造**→审计链可被冒充 | 最小 bearer/API-key；actor 由鉴权身份服务端绑定不接受自报；高风险端点二次确认；绑 127.0.0.1+Origin 校验 |
| G3 | 冲突 | high | §06.4.4(gimmick_rules/constraint fact) vs §03.8(facts injection_mode=always) vs §08.3(config 硬编码)；§02.2.1 fact_type CHECK 无 'constraint' | always-on 禁忌来源**三处互斥** + 幻影 fact_type='constraint'（写不进库） | 统一为 facts + 新增合法 fact_type='constraint'/'taboo' + injection_mode='always'；config 降级为 seed 导入 |
| G4 | 冲突 | high | §07.5 line264 ctx.repos.constraints.always_on()；§02 无 constraints 表；impl/00 RepositoryBundle 无 constraints repo | §07 引用**不存在的 constraints 仓储** | 删 constraints repo，统一为 facts 仓储 always_on 查询(injection_mode='always'+as-of) |
| G5 | 冲突+遗漏 | medium-high | §03.8 always 分支无 valid_from/to/as-of 过滤(对比 detected 分支有)；§06.4.4 称禁忌有生效区间 | 到期禁忌被**永久注入**稳定前缀，且改前缀字节致缓存失效；**禁忌录入/失效流程全程缺失** | always 分支补 as-of 过滤；新增禁忌录入入口(经治理落账)+到期扫描；区间变化按卷边界对齐避免抖缓存 |

---

## 剔除的假阳性（综合者判定，已核验后排除）
- gimmick FSM 列冲突 → 并入 B1（同源）；relationship 无关系表 → 可能是"只做软记忆"的设计本意；爽点兑现与人审"死锁" → 实为 require_human_for 预期代价；get_recall_pack 无预算裁剪 → ToolLoop 侧 truncate_observation 已有 token 上限；单写者内存队列丢写/退场实体回归 → 与 C/F 项重叠。

## 整体诊断
- **不是方向错，是装配松**。绝大多数是"多节对同一对象给了不一致落地"（双签名、列名漂移、产物归属、入参来源），以及"被引用但从未定义"（应用器、迁移器、种子格式、转译层、事件存储）。
- **两条主轴最致命**：A（真相落地链）让一致性引擎从 MVP1 起读空表；C（治理装配）让晋升闸门要么漏判高风险、要么异步候选永停。这两条必须先修。

## 建议修复顺序（与 MVP 对齐）
1. **§10 增 R12**（B 全主题）+ **新增《投影应用器》节并修 §11.7**（A1/A3）→ 解锁 MVP0/MVP1 地基。
2. **裁定 PromotionPolicy.decide 唯一签名 + 异步候选 Gate 入口 + target_type→fact_type + ExtractSkill 归属**（C 全主题）。
3. **补 MVP0 确定性 claim 抽取器 + 误报豁免录入**（D）→ MVP0 Check 可跑。
4. **bible_seed 格式 + schema 迁移器 + 备份恢复含 l0**（F1/F2/F3）→ MVP0 可冷启动可运维。
5. **prompt injection 边界 + REST 鉴权/actor 绑定 + 全局禁忌体系统一**（G）。
6. **运行时契约**（E：autopilot 端点/SSE 续传/转译层/会话预算累加）→ 主要服务 MVP3 全自动，可稍后。
