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
| `undemonstrated_ability` (b) | 解决章的高潮段动用（或出现）了此前从未展示或使用过的金手指 | structural |
| `unpaid_cost` (c) | 本章用了带 `cost_json` 的金手指，提案和正文都没有兑现代价 | local |
| `late_newcomer` (d) | 卷进度超过 `late_newcomer_progress` 后才首次出现的具名实体 | structural |
| `new_ability_ratio` (e) | 本章新增了能力，且本卷"新增能力数 ÷ 旧能力新用法数"超过阈值 | structural |

每条 finding 都带 `span`（名字所在整句，草稿原样子串，两侧各最多 100 字）和一条具体的 `fix`。
(a)–(d) 每项最多 3 条，(e) 每章最多 1 条。文案按 `writing_profile.content_language` 给中文或英文，
英文项目的文案不含任何中文。

## 2. 数据流

`generate_chapter` 的 recall 步写入 `workspace["cfg"] = cfg`（唯一的 orchestrator 改动）→
draft / `_ensure_final_proposals` 产出 `workspace["proposals"]` → CHECK 步 `CraftCheckSkill.run` →
`extensions.run_craft_checks` → `craft_checks`。读取 draft_text、beats、proposals、
`plan_context.current_contract/current_volume`、writing_profile；只读 SQL 查 entities /
entity_aliases / gimmick_rules / gimmick_usage_log / facts / fact_candidates / volumes /
draft_index / drafts_fts / meta_kv。

workspace 缺键时的退路：没有 `plan_context`（治理端点 `/check/craft` 只给 draft/beats/proposals）
就用 `gather_plan_context(conn, N, lookahead_chapters=0)` 按章号只读地取契约与卷信息；没有
`writing_profile` 用 `get_project_writing_profile`；没有 `cfg` 用默认 `PlausibilityConfig()`；任何
`sqlite3.Error`（旧库缺表、缺列）都退化为"无证据"或"无法判断"，保持静默。

注册表不用 `recall_pack`：它的 `gimmick_rules` 只截到 N-1 章，恰好漏掉本章新设定的金手指；
它的 `entities` 也只是推断出的子集。这里直接查库，拿到的是超集。

## 3. 关键判定

**解决章**：契约 `must_resolve` 非空，或契约 beats 含 `payoff_beat`，或本章实际使用的 beats
（planner 生成的，或选定的契约）含 `payoff_beat`。

**高潮段**按 beat 序号比例估算。有 `payoff_beat` 时，取每个 payoff beat 的窗口（第 i/n 个对应
`[i/n − 10%, (i+1)/n + 10%)`）；没有时（仅 `must_resolve`），取正文末尾 `resolving_tail_ratio`
（默认 0.4）。若最后一个 beat 是 hook，再去掉末尾 `min(1/n, tail/2)`：章末新冒出的反派或金手指
是钩子，不是"解决者"。

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

第 0 章的 facts 不算出场（`SeedProposal.valid_from_chapter` 默认 0，是设定库）。早前正文提到过名字
也算铺垫（"听说老祖闭关百年"，本章老祖出手不报）。第 1 章没有"此前"，跳过 a、b、d、e（只做 c）。

**实体来源**：
- 注册表实体：类型为 character / item / faction / concept（地点不会出手解决冲突，不参与）。
- 本章提案提到、注册表里没有的名字（生产链路不写 `entities`，这是 a/d 的主要来源）：只取
  character_trait / relationship / event / item / power_system / knowledge 提案的 `entity` /
  `new.subject` / `new.user_entity` / `new.owner`（item 再加 `new.object`）；location /
  world_rule / style / numeric / misc / constraint 提案不取，且同批里被 location / world_rule /
  style / constraint 提案当主语的名字整批排除——每卷高潮换一个新秘境、新擂台是常态，不是外挂。
  名字要"像专名"（中文 2–8 个汉字；英文 1–4 词且首词大写），并排除无名龙套（"黑衣人""一名
  侍卫""the guard"）。这类名字只在早前正文能证明"从未出现"时才判"新"，新项目和第 1 章永远静默。
- 名字匹配先长后短：落在另一个更长已知名字（含地点、别名）里的命中不算，别名"老祖"不会命中
  "血魔老祖"，"黑风"不会命中地点"黑风谷"。

**金手指"展示过"**（b）：下面任一条成立即算展示过。
- `gimmick_usage_log` 有 `use_chapter < N` 的记录；
- 1..N-1 章有提到它的 canon fact（排除定义它的 `fact_id` / `source_fact_id` 和 `facet=gimmick_rule`）；
- 早前正文提到过它。

一条都不满足时：注册表 `valid_from_chapter >= N`，或注册表里没有、本章有 gimmick_rule 提案，
判"未展示"，措辞为"本章才首次设定"；其余情况要能证明"从未提及"才判，措辞为"此前从未展示或
使用过"。无连接（`conn=None`）时，本章提案定义的能力照样会报。金手指别名取注册表中同名实体的
`entity_aliases`。

**高潮段"动用"还是"出现"**（b）：gimmick_use 提案的 evidence 落在高潮段，或高潮段里名字与未被
否定的使用动词同分句，算"动用"，措辞为"被用来解决问题"。只出现名字（反派叫破、人物回想）仍报，
但措辞放软为"出现在高潮段；若由它解决冲突……"——保留召回（"Starfall answered the call"
这类无词表动词的写法很常见）。名字所在整句只有被否定的使用动词（"想起血遁术的传说，却终究没敢
动用"）不报：主动放弃不是外挂。

**本章"用过"**（c、e）：有 gimmick_use 提案；或名字所在分句（按逗号/分号切）里有使用类动词，且动词前没有否定词。
中文动词如施展、催动、发动、动用、祭出、化作……（看前两字内有无否定；"不得不/不能不"等双重
否定与"忍不住/不断/别人"等伪否定除外）；英文如 use、invoke、activate、unleash、cast、channel……
（前面不能有 not / never / n't）。只提到名字不算用过，例如"想起血遁术的传说""没有动用血遁术"
"想起血遁术，转身催动青冥剑"。

**代价已付**（c）：某条 gimmick_use 提案的 `paid_cost_json` 非空（与 `_apply_gimmick_use` 写入的
形状一致）；或正文出现了从 `cost_json` 抽出的代价词（遍历 dict 的非 schema 键和所有字符串值；中文
剥掉动词、数量前缀和结果后缀，"消耗十年寿元"→"寿元"；英文取 ≥4 字母的实词做前缀词干匹配）。预提交
时 `gimmick_usage_log` 里本章的行属于上次提交的旧正文，不采信；抽不出代价词（如 `{"amount": 3}`）就静默。

**卷进度**（d）：与 `Orchestrator._volume_progress` 同口径（闭区间卷 `(N−start)/(end−start)`）。
craft 层不导入 orchestrator，逻辑在模块内复刻一份，并有测试保证两者一致。卷 `end_chapter` 为
NULL（还在写）时没有进度，跳过 d；已被 a 报过的名字不再重复报。

**比值**（e）：
- 新增 = 本卷 start..N-1 设定的金手指数 + 本章新设定数（提案定义，或注册表里 ≥N 且正文出现）；
- 旧能力新用法 = 本卷内、卷前已有金手指在 `gimmick_usage_log` 的 distinct (gimmick, chapter) 数
  + 本章用到的卷前金手指数；
- 触发条件：本章确有新增（刻意的门槛：不新增能力的章节执行不了这条建议，逐章重复只会堆高 warn 数），
  且新增 ≥ 3，且 新增 ÷ max(1, 旧用法) > `new_ability_ratio_warn`。

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

- 纯规则加只读 SQL，结果按正文位置和名字排序，同一输入重复运行结果一致，可在每轮 revise、polish、
  人工换稿校验中重跑。不写库，不碰 stable prefix，不新增 prompt，与 FakeProvider 路由标记无关。
- 早前正文扫描只在"无结构化证据"时发生，每次检查最多 16 次，超出上限判"无法判断"并静默。

## 6. 局限

- 区分不了"解决者"和"被解决者"：本章才登场就被击败的反派也会被报成 a（措辞有偏差，问题本身也值得一看）。
- 高潮段按比例估算，没有逐 beat 的证据对齐（actuals 在提交后才抽取，预提交阶段拿不到）。
- 使用动词和否定词靠词表，覆盖不全。漏判会少报，误判"用过"只影响 c、e 两项 warn 和 b 的措辞。
- 英文多词名只做"同段共现"近似，只会偏向"已出现"。早前正文检索不做长名遮罩（出现过"血魔老祖"
  会让别名"老祖"算已出现），也只会少报。
- 中文无 jieba（当前进程缺 jieba，或 `meta_kv.tokenizer_version` 记为 fallback-bigram）时，FTS 存的
  是"单字+bigram"重叠串，3 字以上的名字去空格后搜不到，这类名字的正文检索判"无法判断"并静默，
  2 字名照常可判。jieba 是硬依赖，这只是兜底。
- 生产链路目前没有代码写 `entities` 或 `first_appear_chapter`，cold_extract 也不产出 gimmick facet。
  a、d 在生产中主要靠提案名字加早前正文；b、c、e 依赖 seed 或人工录入的 `gimmick_rules`。
- `gimmick_rules.valid_from_chapter` 会被 upsert 覆盖。为此 b 先查早前使用记录和正文痕迹，不只看
  `valid_from`；e 的"本卷新增"仍可能因覆盖而偏多。
