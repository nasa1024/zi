# F1 · 读者承诺账本（Promise Ledger）

> 依据：[23 号调研](../23-中英文小说写作逻辑调研与借鉴.md) §七 P0-1——期待感 = 埋扣子 + 提醒 + 兑现
> （猫腻"大高潮前要挖好扣子"、Sanderson Promises–Progress–Payoff、MICE 后开先关、千幻冰云"开头一长一短，
> 短线 3 万字内揭开"）。目标：伏笔账本从"埋与收"升级为"埋—提醒—推进—兑现"。

## 一、做了什么

| 能力 | 位置 | 性质 |
|---|---|---|
| 休眠承诺告警（最多 3 条，最久休眠在前） | `craft/promise_ledger.py::craft_checks` | 确定性 warn |
| 短线超时告警（累计字数超上限且本章未触及） | 同上 | 确定性 warn |
| 开篇一长一短（第 `opening_window_chapters` 章） | 同上 | 确定性 warn |
| 契约承诺操作落空 / 扣子没挖够 / 张力断崖 / 嵌套顺序 | 同上（读本章契约 `promise_actions`） | 确定性 warn |
| 承诺提醒块（规划 + 起草的动态段） | `draft_brief` | 确定性 |
| 契约 `remind` 落账 + 结算后违规报告 | `settle`（`ext_promise_ledger` 步骤） | 确定性，零 LLM |
| 结算员输入带线型/可见性/父承诺，评估排序防饿死 | `craft/foreshadow_settle.py` | 仍是那 1 次 LLM 调用 |
| API 字段、校验、`/foreshadow/dormant`、`health.dormant_count` | `app/api/craft.py`、`app/models.py` | — |

不新增表/列：v22 基础迁移已给 `foreshadow` 加了 `promise_kind / line_type / visibility / parent_id / remind_every`。
`foreshadow_log.action` 的 CHECK 不变，契约 `remind` 落账为 `mention`。

## 二、数据语义

- **触及（touch）**：埋设章、`last_mentioned_chapter`、`last_advanced_chapter` 的最大值；`chapters_since_touch = N − touch`（不为负）。
- **提醒周期**：显式 `remind_every` 优先；否则 long = `default_remind_every`(5)，short = max(2, 5//2)=2，micro = 1。
- **到期提醒**：读者可见、未关闭（planted/reinforced/misled/overdue）的 long/short 线，`gap ≥ remind_every`。
- **休眠**：同上且 `gap > remind_every × dormant_factor`（默认长线 >10 章、短线 >4 章）。`hidden`（暗线）与 micro 线永不催。
- **排序**（"最久休眠"）：`gap / remind_every` 降序 → 重要度 → gap → 埋设章 → id，全确定性。
- **字数**：区间内各章优先取 `chapter_cards.word_count`，再取 `draft_index` 最新修订，缺失按 zh 3000 字 / en 2000 词兜底。
- **扣子深度**：`1（埋设）+ 本章之前有 mention/advance 的不同章数`（`foreshadow_log` ∪ last_* 列，同章去重、不含埋设章）。
- **大爽点**：`importance ≥ 4` 或长线。
- **promise_kind**：推荐词表 `mice_milieu/mice_inquiry/mice_character/mice_event/clue/vow/grudge/treasure/info_gap/relationship/other`；
  API 把词表内写法规范化（`"MICE Inquiry"` → `mice_inquiry`），词表外原样保留，逻辑上按 `other` 处理。

## 三、正文提及判定（`DraftMatcher`）

依次尝试 label 与"足够短的描述"（中文 3–12 字 / 英文 2–6 词）：
1. 精确：中文忽略空白标点的子串；英文按整词短语（`Art` 不会命中 `start`）。
2. 模糊兜底（同句内）：中文 ≥4 字的短语，字符 bigram 覆盖 ≥50%（同 `foreshadow_settle` 的 bigram 思路）；
   英文至少 2 个内容词且覆盖 ≥50%（去停用词、粗略去复数）。
命中时返回所在句作为证据片段（结算落 `foreshadow_log.evidence`）。

## 四、数据流

```
recall 步骤 ─ build_draft_briefs → promise_ledger.draft_brief
    │   ├ 写 workspace["promise_config"]（本章 craft 检查用同一份阈值）
    │   └ 返回「## 承诺提醒（本章需自然呼应，不要生硬复述）」块 → workspace["craft_brief"]
    │        planner：future plan 之后；drafter：契约块之后、文风锚点之前（永不进稳定前缀）
CHECK / 每轮 revise / polish 后 / 人工换稿校验 ─ CraftCheckSkill → promise_ledger.craft_checks（只读）
COMMIT 后 _settle_chapter ─ foreshadow_settle（LLM，1 次）→ ext_promise_ledger.settle（确定性）
    └ detail_json["extensions"]["promise_ledger"] = {reminded, violations, payoffs, dormant[, error|skipped]}
```

承诺提醒块内容：最多 `inject_dormant_limit`(2) 条到期提醒的读者承诺（`- 血仇（第1章埋下，已 8 章未提及）：灭门真凶仍未现身`），
再加本章契约里能映射到账本的承诺操作（补埋设章、描述、"先收束内层承诺"、"铺垫尚浅"提示）。
仅有标签、映射不到账本的操作已由契约块渲染，不重复。英文项目渲染 `## Promise reminders (weave in naturally, do not recap)`。

## 五、craft 检查明细（全部 `severity=warn`，category `craft.<check>`）

| check | 触发 | 静默条件 |
|---|---|---|
| `promise_dormant` | 休眠的读者长/短线 | 正文已提及；本章契约已安排触及；同承诺已报短线超时 |
| `promise_short_overdue` | 读者短线自埋设章起（不含本章）字数 > `short_line_max_chars`（en：`short_line_max_words`） | 正文已触及；契约已安排 |
| `promise_opening` | N == `opening_window_chapters` 时，读者可见承诺（任何状态）缺长线或短线 | 账本完全没用过（无行且无契约操作）；未建档的计划 `plant` 可抵一个空缺 |
| `promise_contract_missing` | 契约 remind/advance/payoff 的承诺在正文中找不到 | 只给了未知 promise_id |
| `promise_thin_setup` | 计划兑现的大爽点扣子深度 < `big_payoff_min_touches` | — |
| `promise_payoff_cliff` | 计划兑现条数 > `max_payoffs_per_chapter` | 本章是卷末章（研究文档"卷末章除外"） |
| `promise_nesting` | 计划兑现外层，内层仍开着 | 内层也在本章计划兑现 |

契约 `promise_actions` → 账本行的映射：`promise_id` → 精确 label → label 相似度 ≥0.5（未关闭的行优先）。

## 六、结算步骤（`settle`）

1. 契约 `remind` 且最终稿提及 → `last_mentioned_chapter = max(原值, N)` + 一条 `mention` 日志（证据为命中句）。
   若该承诺本章已有 mention/advance/payoff 日志（LLM 结算或上次重跑写的）则跳过——重跑幂等。写入包在 SAVEPOINT 里。
2. 报告：`nesting`（本章兑现的外层仍有未关内层）、`payoff_cliff`、`thin_setup`、`payoffs`（`paid_off_chapter=N` 的行数）、
   `dormant`（进入下一章时的休眠数）。
3. 数据问题（缺表、坏连接、脏值）只写 `error`，不抛异常，不会让章节变成 `state_degraded`。

## 七、foreshadow_settle 改动

- 交给结算员的未解伏笔仍 ≤20 条，但排序改为：正文点名的 → 读者可见 → 最久未触及 → 最早埋设 → id，
  被提醒块注入的休眠长线不会再被 `ORDER BY planted_chapter LIMIT 20` 挤掉。老库（无 v22 列）保持旧口径。
- 每行附 `［长线·读者可见］` 标签与 `（父承诺：X）`；`## 未解伏笔列表` 标题与"单次调用"不变。
- 结算仲裁新建的钩子默认 `line_type='short'`、`visibility='reader'`。

## 八、API

- `POST/PATCH /foreshadow` 接受 `promise_kind`(≤40)、`line_type`(long|short|micro)、`visibility`(reader|hidden)、
  `parent_id`、`remind_every`(1–1000)；响应全部回显。父承诺必须存在、不能是自身、不能成环（422）。
  PATCH 中 `promise_kind/parent_id/remind_every` 显式传 `null` 表示清空，其余字段 `null` 仍表示不变（兼容旧行为）。
- 删除外层承诺时，其内层的 `parent_id` 置空（否则外键阻止删除）。
- `GET /{pid}/foreshadow/dormant?as_of_chapter=N`（默认下一章）：休眠承诺 + `last_touch_chapter / chapters_since_touch /
  effective_remind_every / dormant_after`；路由声明在 `/foreshadow/{id}` 之前。
- `GET /foreshadow/health` 新增 `dormant_count`（其余字段不变）；前端类型 `ForeshadowHealth.dormant_count?` 可选。

## 九、配置（`cfg.promises`）

`enabled`、`default_remind_every`=5、`dormant_factor`=2、`short_line_max_chars`=30000、
**`short_line_max_words`=20000（本特性新增，英文项目）**、`opening_window_chapters`=3（设 0 关闭）、
`max_payoffs_per_chapter`=2、`big_payoff_min_touches`=2、`inject_dormant_limit`=2。
`craft_checks` 拿不到 cfg，读 `draft_brief` 写进 workspace 的同一份配置；没有时（治理端点、人工换稿新建的工作区）用默认值。

## 十、确定性 vs LLM

全部检查、提醒块、结算步骤都是确定性的，不新增 LLM 调用；唯一的 LLM 仍是原有的 `foreshadow_settle`（只改了输入）。

## 十一、局限

- "触及"判定基于标签/短描述的字面与 bigram 匹配：别名、代称、纯意象式提醒会漏判（会多报休眠），长描述不参与匹配。
- 正文能否算"推进/兑现"无法确定性区分：短线超时检查把"正文提及"视为已处理，交给 LLM 结算细分。
- 契约检查依赖 planner 写出 `promise_actions`；只写 label、与账本标签差异过大时映射不到账本行，只做"正文落空"检查。
- 重写更早章节时只按 `planted_chapter ≤ N` 过滤，last_* 列反映的是当前账本状态，可能把后续章的触及算进来（只会少报）。
- 人工换稿（select-candidate）不会重跑结算，账本不随换稿回滚（与既有伏笔结算一致）。
- 每章休眠告警上限 3 条而提醒块只注入 2 条，休眠承诺很多时会持续告警；开启质量门时 ≥3 条 warn 会触发润色。
