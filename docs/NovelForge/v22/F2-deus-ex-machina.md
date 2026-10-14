# F2 "天降外挂"拦截器（deus-ex-machina guard）

> 依据：调研文档 23 §七 P0-2。乌贼"不能随意加入新东西或新外挂来解决问题"；Sanderson 第一定律
> （读者理解多少，能力才能解决多少冲突）、第二定律（Limitations > Powers）、第三定律（先扩展再新增）；
> Brooks 第二情节点后"no newcomers"。实现：`novelforge/craft/deus_ex_machina.py`。

## 1. 做什么

在 `CraftCheckSkill` 的 v22 扩展接缝（`craft_checks(ctx)`）里跑五项**确定性、只读、默认 warn**
的检查。它们不增加任何 LLM 调用，也不产生 block，所以不会驱动 revise，也不计入 autopilot 降级。
warn 会进入 `outcome.issues`、SSE 计数和润色输入（≥3 条 craft warn 时触发 polish）。

| check（category = `craft.deus_ex_machina.<check>`） | 条件 | repair_scope |
|---|---|---|
| `newcomer_resolver` (a) | 解决章的高潮段出现了本章首次登场的具名实体 | structural |
| `undemonstrated_ability` (b) | 解决章的高潮段动用了此前从未展示或使用过的金手指 | structural |
| `unpaid_cost` (c) | 本章用了带 `cost_json` 的金手指，提案和正文都没有兑现代价 | local |
| `late_newcomer` (d) | 卷进度超过 `late_newcomer_progress` 后才首次出现的具名实体 | structural |
| `new_ability_ratio` (e) | 本章新增了能力，且本卷"新增能力数 ÷ 旧能力新用法数"超过阈值 | structural |

每条 finding 都带 `span`（名字所在整句，草稿原样子串，两侧各最多 100 字）和一条具体的 `fix`。
(a)–(d) 每项最多 3 条，(e) 每章最多 1 条。文案按 `writing_profile.content_language` 给中文或英文，
英文项目的文案不含任何中文。

## 2. 数据流

```
generate_chapter
  ├─ recall：workspace["cfg"] = cfg          ← 唯一的 orchestrator 改动（读 cfg.plausibility）
  ├─ plan / draft / _ensure_final_proposals  → workspace["proposals"]
  └─ CHECK：CraftCheckSkill.run → extensions.run_craft_checks → deus_ex_machina.craft_checks
        读取：draft_text、beats、proposals、plan_context.current_contract/current_volume、writing_profile
        只读 SQL：entities / entity_aliases / gimmick_rules / gimmick_usage_log / facts /
                  fact_candidates / volumes / draft_index / drafts_fts
```

workspace 缺键时的退路：
- 没有 `plan_context`（治理端点 `/check/craft` 只给 draft/beats/proposals）：用
  `gather_plan_context(conn, N, lookahead_chapters=0)` 按章号只读地取契约与卷信息；
- 没有 `writing_profile` 就用 `get_project_writing_profile`；没有 `cfg` 就用默认 `PlausibilityConfig()`；
- 任何 `sqlite3.Error`（旧库缺表、缺列）都退化为"无证据"或"无法判断"，保持静默。

注册表不用 `recall_pack`：它的 `gimmick_rules` 只截到 N-1 章，恰好漏掉本章新设定的金手指；
它的 `entities` 也只是推断出的子集。这里直接查库，拿到的是超集。

## 3. 关键判定

**解决章**：契约 `must_resolve` 非空，或契约 beats 含 `payoff_beat`，或本章实际使用的 beats
（planner 生成的，或选定的契约）含 `payoff_beat`。

**高潮段**按 beat 序号比例估算。有 `payoff_beat` 时，取每个 payoff beat 的窗口（第 i/n 个对应
`[i/n − 10%, (i+1)/n + 10%)`）；没有时（仅 `must_resolve`），取正文末尾 `resolving_tail_ratio`
（默认 0.4）。若最后一个 beat 是 hook，再去掉末尾 `min(1/n, tail/2)`：章末新冒出的反派或金手指
是钩子，不是"解决者"。gimmick_use 提案的 evidence 若落在高潮段里，也算在高潮段使用。

**首次出现**（a、d 共用）。原则是"此前留有读者可见的痕迹就算铺垫过"，宁漏勿误报。
下面任一条成立，即算此前已出现：
- `entities.first_appear_chapter < N`；
- 第 1..N-1 章有非 retcon 的 `facts`（`entity_id` 或 `subject` ∈ 名字和别名）；
- 第 1..N-1 章有未被否决的 `fact_candidates`（`entity_id`、`$.entity` 或 `$.new.subject` 命中）；
- 第 1..N-1 章正文（`drafts_fts`，只含各章当前修订）提到过名字或别名。中文去掉分词空格后做
  `instr` 精确子串匹配，与 jieba 的切分无关；英文用同一个 `tokenize()` 生成词元，同段落里出现
  名字的全部词元即算提到。

一条痕迹都没有时：`first_appear_chapter >= N` 直接判"新"（正面证据）。否则要能证明"从未提及"
才判"新"，条件是 1..N-1 章里凡在 `draft_index` 有草稿的，都已进入 `drafts_fts`；证明不了就判
"无法判断"并静默。

补充说明：
- 第 0 章的 facts 不算出场：`SeedProposal.valid_from_chapter` 默认为 0，属于设定库，读者看不到。
- 早前章节提到过名字也算铺垫。比如"听说老祖闭关百年"，到本章老祖出手，就不报。
- 第 1 章没有"此前"可言，跳过 a、b、d、e（只做 c）。冷启动导入的正文经 `_persist_draft` 写入，同样有 FTS 索引。

**实体来源**：
- 注册表实体：类型为 character / item / faction / concept（地点不会出手解决冲突，不参与）。
- 本章提案提到、注册表里没有的名字：取自 `entity` / `new.subject` / `new.user_entity` /
  `new.owner`，以及 item 提案的 `new.object`。名字要"像专名"（中文 2–8 个汉字；英文 1–4 词且
  首词大写），并排除无名龙套（"黑衣人""一名侍卫""the guard"）。这类名字只有在早前正文能证明
  "从未出现"时才判"新"，所以全新项目和第 1 章永远静默。

**金手指"展示过"**（b）：下面任一条成立即算展示过。
- `gimmick_usage_log` 有 `use_chapter < N` 的记录；
- 1..N-1 章有提到它的 canon fact（排除定义它的 `fact_id` / `source_fact_id` 和 `facet=gimmick_rule`）；
- 早前正文提到过它。

一条都不满足时：注册表 `valid_from_chapter >= N`，或注册表里没有、本章有 gimmick_rule 提案，
判"未展示"，措辞为"本章才首次设定"；其余情况要能证明"从未提及"才判，措辞为"此前从未展示或
使用过"。无连接（`conn=None`）时，本章提案定义的能力照样会报。金手指别名取注册表中同名实体的
`entity_aliases`。

**本章"用过"**（c、e）：有 gimmick_use 提案；或名字所在分句（按逗号/分号切）里有使用类动词，且动词前没有否定词。
中文动词如施展、催动、发动、动用、祭出、化作……（看前两字内有无否定）；英文如 use、invoke、
activate、unleash、cast、channel……（前面不能有 not / never / n't）。只提到名字不算用过，例如
"想起血遁术的传说""没有动用血遁术""想起血遁术，转身催动青冥剑"。

**代价已付**（c）：下面任一条成立即算已付。
- 某条 gimmick_use 提案的 `paid_cost_json` 非空，与 `world/projection.py::_apply_gimmick_use`
  写入的形状一致；
- 正文出现了从 `cost_json` 抽出的代价词。抽取时遍历 dict 的非 schema 键和所有字符串值；中文剥掉
  动词、数量前缀和结果后缀（"消耗十年寿元"→"寿元"，"灵力耗尽"→"灵力"）；英文取 ≥4 字母的实词，
  做前缀词干匹配（memory 能匹配 memories）。

预提交时 `gimmick_usage_log` 里本章的行属于上一次提交的旧正文，不采信。`cost_json` 若抽不出可检索
的词（如 `{"amount": 3}`），判"无法判断"并静默。

**卷进度**（d）：与 `Orchestrator._volume_progress` 同口径（闭区间卷 `(N−start)/(end−start)`）。
craft 层不导入 orchestrator，逻辑在模块内复刻一份，并有测试保证两者一致。卷 `end_chapter` 为
NULL（还在写）时没有进度，跳过 d；已被 a 报过的名字不再重复报。

**比值**（e）：
- 新增 = 本卷 start..N-1 设定的金手指数 + 本章新设定数（提案定义，或注册表里 ≥N 且正文出现）；
- 旧能力新用法 = 本卷内、卷前已有金手指在 `gimmick_usage_log` 的 distinct (gimmick, chapter) 数
  + 本章用到的卷前金手指数；
- 触发条件：本章确有新增，且新增 ≥ 3，且 新增 ÷ max(1, 旧用法) > `new_ability_ratio_warn`。

"本章确有新增"是刻意加的门槛：不新增能力的章节执行不了这条建议，卷级提示若逐章重复只会堆高 warn 数。

## 4. 配置（`cfg.plausibility`，`PlausibilityConfig`）

| 字段 | 默认 | 作用 |
|---|---|---|
| `enabled` | True | 总开关 |
| `late_newcomer_progress` | 0.75 | 卷进度超过此值后，首次出现的新实体触发 d；文案中的"卷末 X%"由它换算 |
| `new_ability_ratio_warn` | 1.0 | e 的比值阈值 |
| `soft_magic` | False | 软魔法体系：b 改用"提示"措辞，强调人物选择和牺牲，但仍为 warn |
| `resolving_tail_ratio` | 0.4 | 本特性新增：无 payoff beat 的解决章，高潮段占正文末尾的比例 |

orchestrator 在 recall 步写入 `workspace["cfg"] = cfg`。治理端点和人工换稿校验没有这个键，用默认配置。

## 5. 确定性与成本

- 全部检查是纯规则加只读 SQL，结果按正文位置和名字排序。同一输入重复运行结果完全一致，
  可以在每轮 revise、polish 和人工换稿校验中放心重跑。
- 早前正文的全表扫描只在"无结构化证据"时才发生，每次检查最多 16 次，超出上限就判"无法判断"
  并静默。已出场的名字通常在前几行就能命中。
- 不写库，不碰 stable prefix，不新增 prompt，与 FakeProvider 的路由标记互不影响。

## 6. 局限

- 区分不了"解决者"和"被解决者"：本章才登场就被击败的反派也会被报成 a（措辞有偏差，问题本身也值得一看）。
- 高潮段按比例估算，没有逐 beat 的证据对齐（actuals 在提交后才抽取，预提交阶段拿不到）。
- 使用动词和否定词靠词表，覆盖不全。漏判会少报，误判"用过"只影响 c、e 这两项 warn。
- 英文多词名只做"同段共现"近似，只会偏向"已出现"。中文在无 jieba 的回退分词下，3 字以上、又不在
  用户词典里的名字可能检索不到，于是判为"无法判断"。
- 生产链路目前没有代码写 `entities` 或 `first_appear_chapter`，cold_extract 也不产出 gimmick facet。
  a、d 在生产中主要靠提案名字加早前正文；b、c、e 依赖 seed 或人工录入的 `gimmick_rules`。
- `gimmick_rules.valid_from_chapter` 会被 upsert 覆盖。为此 b 先查早前使用记录和正文痕迹，不只看
  `valid_from`；e 的"本卷新增"仍可能因覆盖而偏多。
