# F6 · 多章连写（Batch drafting）设计说明

> 产品方向："不是一章一章写，而是尽可能多写几章"。长输出模型（Gemini 4 long decode，
> `CapabilitySet.long_output=True`）一次调用起草连续 N 章，然后**逐章、按顺序**走原有的
> check → revise → quality → persist → actuals → settle 流水线，确定性连续性/工艺/计划守卫一章不少。
>
> 研究约束（23 号调研 §五 Book-Writing：章计划遵循率第 1 章 73.1% → 末章 50.8%）：
> 长生成中计划遵循逐章衰减，所以每章在连写提示里**各自携带完整契约 + 目标 + 字数 + Beat Sheet**，
> 输出按章切分后逐章校验；某章不合格就丢弃其后依赖章、从该章重写。23 号 §九"不用 1M 输出一次写整卷"
> 的告诫仍成立：单次窗口默认 ≤10 章，章仍是校验与提交的单位。

## 1. 模块与数据流

| 模块 | 作用 |
|---|---|
| `control_plane/batch.py` | `batch_capacity` 资格判定；`run_batch` 编排（窗口、逐章提交、失败策略、成本摊分、事件） |
| `skills/chapter_batch_draft_skill.py` | `chapter_batch_draft` skill：组装连写 prompt、单次 STRONG 调用、按章节标记切分 + 逐章确定性校验 |
| `control_plane/orchestrator.py` | `generate_chapter(predrafted_text=, predrafted_beats=, batch_id=)` 接缝；`gather_recall_pack` / `compose_stable_context` 两个共用函数（单章与连写的稳定前缀同源） |
| `app/api/orchestrator.py` | `POST /pipeline/batch`、`POST /pipeline/batch/stream`（SSE）、`GET /pipeline/batches[/{id}]` |
| `app/autopilot_manager.py` | 挂机连写在长输出模型下改走 `run_batch`；`batch_size` 启动参数 |
| `db/l0.py` | `sweep_stale_batches`：崩溃残留的 `running` 批次 → `failed` |
| `web/…/BatchDraftPanel.tsx` | Studio「📚 多章连写」控件（按章分组的实时进度） |

```
run_batch(start, count)
 ├─ capacity = batch_capacity(gateway, cfg, profile)
 ├─ capacity < max(2, min_chapters) → 顺序模式：逐章 generate_chapter（遇失败章即停）
 └─ 连写模式，循环窗口 [next, next+capacity)（写作配置在窗口内切换则截断）：
     1. INSERT draft_batches(status=running, origin)
     2. 起始章上下文 as_of(start-1)：召回包 → 稳定前缀 / 当前世界状态 / craft_brief
     3. 逐章：gather_plan_context(k) 渲染契约；k>start 另算该章自己的 craft_brief（F1/F3/F5，
        as_of(start-1) 账本近似）；节拍 = 已选/已计划契约节拍，否则内存内跑 planner（带该章 brief）
     4. chapter_batch_draft：一次调用（gateway 的预算 ×N）→ {k: 正文} + {k: 拒绝原因}
     5. INSERT draft_batch_chapters：可用前缀 pending；首个缺口 rejected；其后 discarded
     6. 逐章（每章全新 orchestrator/ledger）generate_chapter(k, predrafted_text, predrafted_beats, batch_id)
     7. 窗口成本平摊到本窗口接受章（ChapterOutcome + pipeline_run.tokens/usd_spent）
     8. 失败 → 作废后缀、重写（计 regenerations；连续无进展次数受上限约束）；否则继续下一窗口
```

## 2. 资格判定（确定性）

`batch_capacity(gateway, cfg, profile)`：`cfg.batch.enabled=False`、gateway 为 None 或
`capabilities()` 抛错、`require_long_output` 且 STRONG 模型无 `long_output` → **1**；否则
`min(max_chapters, max_tokens_out // 单章预算)`。单章可见输出预算按 profile 允许的上限留足：
中文 `1.6 × max(draft_target_chars, chapter_word_range[1]) + 1024`（默认 5000 字 → 9024；
提示里的"约 3000 字"只是目标，N 章共用一个输出上限，按目标字数算会截断窗口尾章），
英文 `chapter_word_range[1] × 1.8 + 1024`（思考 token 由 Gemini provider 在其上另加余量）。容量 < `max(2, min_chapters)` 走顺序模式。
FakeProvider 声明 `long_output=False`，所有旧测试保持原路径。

## 3. 连写 prompt 与输出协议

user 消息顺序：起始章**稳定前缀**（与单章 prompt 逐字节一致，`CacheHint(user_prefix_chars=len(stable))`）
→ `## 世界状态（当前）` → 起始章 `craft_brief` → 每章一节 `## 连写第 i/N 章 · 第 k 章`
（该章契约整体降一级标题嵌入 → k>start 时 `### 第 k 章动态提示`：该章自己的 F1/F3/F5 brief，
标题降两级、同窗口内此前已出现过的逐字相同条目只说一次 → `### 第 k 章写作要求`：目标与字数 →
`### 第 k 章 Beat Sheet`）
→ `## 连写任务` / `## Batch Writing Task`。system 为"连写助手 / serial drafting assistant"。
所有路由标记（`本章任务`、`## Chapter Task`、`规划任务`、`未解伏笔`、`本章目标：`、`Chapter goal: `、
`草稿：` 等）都不出现；英文项目整段无中文标签。

输出协议：`【第12章·开始】…【第12章·结束】` / `=== CHAPTER 12 START === … === CHAPTER 12 END ===`。
解析器（`parse_batch_output`，确定性）两种标记都认，容忍：缺失的结束标记（截到下一个标记或文末）、
粗体/标题前缀、空白、markdown 围栏、乱序、范围外章节、中文数字章号（`【第十二章·开始】`、
`一百零三`、逐位 `一〇三`）、英文 `===`/`---`/`###` 围栏与 `CHAPTER 12: START` 写法。逐章校验顺序：非空 → 未截断（`stop_reason=max_tokens`
时延伸到文末且无后续标记的片段判为半截章）→ `language_matches` → `count_content_units >= hard_min_words`；
拒绝原因 `missing | empty | truncated | language_mismatch | too_short`。

## 4. 编排器接缝

`generate_chapter` 新增三个关键字参数，缺省时行为逐字节不变：
- `predrafted_text`：RECALL / 计划上下文 / pacing / craft_brief 照常（as_of(k-1)，此时 k-1 已提交）；
  PLAN 优先 `predrafted_beats`（`planner_source="batch"`），否则锁定契约，否则 planner；DRAFT 跳过，
  `candidate_report = {"selected_by": "batch", "batch_id": …}`；空稿检查之后流程完全不变。
- `batch_id`：写入 `pipeline_run.batch_id`（老库无列时静默跳过）。
- 顺带修复：catch-all `except` 现在回滚半截事务并以 `_fail_pipeline_run(…, "exception")` 把本次 run
  标为 `crashed`（含 `error_type`），返回的 `ChapterOutcome` 也带上 `run_id`，不再留下 `running` 僵尸行。

## 5. 失败策略

| 情况 | 处理 | 重写起点 |
|---|---|---|
| 窗口首个缺口章（切分失败） | 缺口章 `rejected`（原因），其后 `discarded: after_rejected:k` | 缺口章 |
| 第 k 章 `ok=False`（未落库） | k `rejected`（run 已 crashed），其后 `discarded: after_failed:k` | k |
| 第 k 章落库但修订后仍有 `block` | 保留 k，其后 `discarded: after_blocked:k`（后续章照着修订前的 k 写成） | k+1 |
| 第 k 章正文落库后才抛异常（`ok=False`，但 `draft_index` 已有本次新修订） | 视同结算降级：`ok=True`、`state_degraded=True`，k `accepted`（不重写，避免重复修订轮与二次结算） | — |
| 末章有 block / 窗口全部接受 | 不作废任何正文，下一窗口照常续写（不计重写） | — |
| `cancel_cb()` 为真（每章与每次连写调用前检查） | 剩余章 `discarded: canceled`，状态 `canceled` | — |
| 连写调用异常（含熔断）/ 回调异常 | 批次 `failed`，`error_kind=circuit_broken|exception`，不重写 | — |

重写次数上限 `cfg.batch.max_regenerations` 按**连续无进展**计：窗口接受 ≥1 章即清零（挂机把整段会话当作
一次 run，按全程累计会让长会话在第 3 次互不相干的偶发失败时报错停止）；卡在同一章连续重写超限即停，
整体状态 `partial`（有接受章）或 `failed`。`BatchOutcome.regenerations` / 事件 `attempt` 仍报告全程总次数。
未处理正文只留在 `draft_batch_chapters`，从不进入 `draft_index`，因此 `next_chapter_no` 与挂机断点续写不受影响。
每次连写调用（含重写/后续窗口）对应一行 `draft_batches`，`detail_json` 记 `root_batch_id / kind
(initial|continue|regenerate) / attempt / accepted / parse_rejects / thrown / failure_reason`。

## 6. 预算与成本

- 连写调用在 `make_orchestrator(budget_scale=N)` 返回的 gateway 上执行：单章预算 ×N（熔断器按
  `max_tokens // 2` 预留，单章额度不够）；planner 补规划也计入该次成本。
- 逐章校验每章一个**全新** orchestrator/ledger：避免 `revise_rounds` 累计熔断与预算串章。
- 窗口结束后把该次调用的 tokens/usd 平摊到本窗口接受章（整数 token 余数给前几章），
  同步 `UPDATE pipeline_run SET tokens_spent=tokens_spent+?, usd_spent=usd_spent+?`；整窗无接受章时
  成本只记在 `draft_batches` 与 `BatchOutcome.usage_*`。
- 挂机：`batch_draft` 事件发生时把该次调用成本计入会话预算并刷新心跳（长调用防 TTL 误杀）；
  逐章回调只计本章流水线成本，两者相加 = 各章 `pipeline_run` 之和，不重复计算。

## 7. API / SSE / 挂机 / 前端

- `POST /{pid}/pipeline/batch`（同步）与 `/pipeline/batch/stream`（SSE）：请求 `BatchRunRequest
  {start_chapter? = 下一章, chapters 1..30 = cfg.batch.max_chapters, chapter_goals?, interaction_mode?,
  mode/budget/n_candidates/quality_check 同单章}`，响应 `BatchRunResponse`。
- SSE 事件：`batch_start` → `batch_draft`（每次连写调用后，含切分结果与成本）→ `stage`（**带 chapter**）…
  → `chapter_done` … →（`batch_discarded` → `batch_regenerate` …）→ `batch_done`，随后关流；异常推 `error`。
  `/pipeline/run` 与 `/pipeline/run/stream` 不变（仍是单个 `done`）。SSE 客户端断开时当前章完成后停止。
- `GET /{pid}/pipeline/batches`（最近批次 + 各状态计数）与 `/pipeline/batches/{id}?include_text=`。
- 挂机：`AutopilotStartRequest.batch_size`（None=自动=容量，1=旧逐章循环），写入 `req_json` 并随 resume 继承。
  启动时探测 gateway 容量 ≥ 门槛才走 `run_batch`；逐章记账/降级/暂停逻辑抽成唯一的 `_account_chapter`
  （两条路径共用），取消/降级/预算检查抽成 `_gate_before_chapter` 并作为 `cancel_cb`。连写模式下未落库的
  失败章只记成本、不计完成数、不推进断点（会被重写）。gateway 为 None（测试）时与旧循环行为一致。
- Studio：「📚 多章连写」（章数默认 5，按章分组的 stage 进度、丢弃/重写日志、汇总）；挂机表单新增「每批章数」。
- 启动期：`init_db` 与注册表首次打开项目时 `sweep_stale_batches`，残留 `running` 批次 → `failed`
  （`failure_reason=interrupted`），其 `pending` 章 → `discarded`。

## 8. 配置（`cfg.batch`，`BatchConfig`）

`enabled=True`、`max_chapters=10`、`min_chapters=2`、`require_long_output=True`、`max_regenerations=2`。

## 9. 确定性 vs LLM

- 确定性：容量判定、prompt 组装、标记切分与逐章校验、可用前缀/作废/重写决策、成本摊分、sweep。
- LLM：一次连写调用；无契约节拍时的 planner（每章一次，as_of 起始章）；其后每章原有流水线调用不变。
  每章的 check/revise/settle 调用数与单章完全相同，只是省掉了每章的起草调用。

## 10. 局限

- 窗口内第 k>起始章的召回与世界状态是 as_of(起始章-1) 的近似（提示词层面）；逐章校验仍严格 as_of(k-1)。
- 第 k>起始章的动态提示同样按 as_of(起始章-1) 的账本计算：F1 到期提醒在窗口内到期章出现（同一承诺在
  后续章的提醒间隔数会变，仍会重复列出，提示语注明"本批前文已处理过的可略过"）；F3 的连续低谷/顺境
  依赖同批前序章尚未落库的极性，无法预知（只有按 k-1 章计划判断的余韵提示）；F5 待补反应同理。
- 无契约节拍时内存内 planner 看不到同批前序章的节拍（只看各自的计划上下文与目标）。
- 修订/润色改写了第 k 章（但未残留 block）时，第 k+1 章仍按原稿续写；依赖第 k+1 章自己的连续性校验兜底。
- `n_candidates` 只在顺序模式生效（连写模式每章只有一稿，`select-candidate` 不适用）；挂机按规范
  `batch_size=None` 即自动连写，长输出模型下需要多候选择优时显式传 `batch_size=1`。
- SSE 断开即停（当前章完成后），需要后台长跑请用挂机；同步 `/pipeline/batch` 无取消。
- 英文路径的端到端只有单元级覆盖（prompt、skill、解析）；集成测试使用中文项目。
