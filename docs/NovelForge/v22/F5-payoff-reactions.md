# F5 · 爽点三段式：动作 → 在场者反应 → 收获重要性展示

> 依据：docs/NovelForge/23 §七 P0-5。猫腻"'自己赢'不是快感的来源，'看对方怎么输'才是"；
> 阅文"剧情 = 动作 + 反应"；起点编辑子良"目标—行动—收获—**收获重要性展示**"。
> v22 之前 `payoff_binding` 只校验"有没有状态写回"，不校验"有没有人看见、有没有反应"。

## 做了什么

| 部件 | 位置 | 性质 |
|---|---|---|
| 反应检测器 `detect_reactions` | `craft/payoff_reactions.py` | 纯函数、确定性、无 LLM |
| actuals 回填 `enrich_actuals` | 同上（扩展点） | 确定性、无 DB |
| 工艺检查 `craft_checks` | 同上（扩展点） | 只读、全部 `warn` |
| 起草/规划提示 `draft_brief` | 同上（扩展点） | 只读、动态段 |
| 结算报告 `settle` | 同上（扩展点） | 只读、确定性 |
| 打脸需要在场者 | `craft/payoff_binding.py` | `warn`，不新增 block |

## 数据流

```
契约 payoff_beat.witnesses（v22 foundation：beats/chapter_plan_version_beats.witnesses_json）
   │
   ├─ 召回步骤 build_draft_briefs → draft_brief
   │     ① 本章爽点三段式提醒（点名在场者，自动剔除主角）
   │     ② 窗口内上一章爽点"欠余波"提醒
   │     ③ 信息差机会（knowledge_edges as of N-1）
   │   → workspace["craft_brief"]：planner 在未来计划之后、drafter 在契约之后渲染，
   │     绝不进入稳定前缀；同时把 cfg.payoff 抄到 workspace["payoff_reactions_cfg"]
   │
   ├─ CHECK（每轮 craft_check 都跑）→ craft_checks：三类 warn（见下）
   │     "欠余波"判定只读重载窗口内前几章已提交的 L0 正文（draft_index 最新修订）再跑检测器
   │
   ├─ 落盘后 extract_chapter_actuals → enrich_actuals
   │     契约 payoff beat 的 witnesses + 群像 → ChapterActuals.reactions
   │     → chapter_actuals.payload_json（evaluate_and_persist_alignment 两个调用点都生效）
   │
   └─ _settle_chapter → settle：{"payoff_beats","reactions","witnesses"（已剔除主角）,
         "reactors","witnesses_missing","source"} → pipeline_run.detail_json.extensions.payoff_reactions
```

## 反应检测（确定性）

1. **词表**（中/英各一套，带态度类别 `shock/fear/submission/awe/silence/regret/resentment`，
   写入 `ReactionEvidence.attitude_shift`）：震惊、愣住、脸色大变、倒吸一口凉气、不敢置信、目瞪口呆、
   跪下/跪倒、惊呼、哗然、低下头、改口、敬畏、骇然、面如死灰、冷汗、噤声、咬牙、后悔…；
   gasped、paled/went pale、froze、flinched、stunned、fell silent、knelt、bowed his head、
   swallowed hard、backed away、couldn't believe…
   **礼节与身法不算反应**：抱拳/拱手/躬身/行礼须接认输/赔罪/求饶；"跪"须带下/倒/地/伏/拜/了
   （排除"跪坐"）；后退须带受惊限定（连退/踉跄后退/退了N步）；英文 stared 须 dumbly/in disbelief…，
   bowed 须 his head/in defeat…，erupted 须 in cheers/applause/an uproar，stepped back 须 in fear…
2. **反应者**：beat 的 witnesses（检查时再并入 `entity_aliases` 别名；英文名额外匹配去掉称谓后的
   首/尾词，如 Elder Wang → Wang）+ 通用群像（众人/全场/围观者/所有人…；everyone/the crowd/the hall…）。
3. **归属**（按句、分句结构，而非"同句共现"）：
   - A 同分句、cue 之前的主语（含并列"王长老、赵师兄"）；主语是主角时取最近的非主角提及
     （"林凡看着王长老脸色大变" → 王长老），但宾语前是"向/朝/对/冲/给/替 · to/toward/at"时
     动作仍属主角（"林凡向王长老道歉"不算王长老的反应）；
   - B 定语式"脸色大变的王长老 / a stunned Elder Wang"；
   - C "对方/对手/his opponent" 只指向非主角（本句或同段前两句）；
   - D 承前省略主语"王长老看着林凡，脸色大变"（被承接的主语须在其分句开头 ≤4 字/≤2 词）；
   - E 代词承接同段上一句"王长老盯着碎玉。他脸色大变。"
4. **主角永不算反应者**："林凡震惊地看着王长老" → 无反应；规划误把主角列为 witness 也会被剔除。
   主角来自 `meta_kv['protagonist']`（自由文本中最早出现的角色名/别名），否则 `chapter_cards.pov_entity_id`。
5. **引号内一律忽略**（“王长老，你后悔了吗？”是嘲讽，不是反应）。
6. 证据区间为所在句（超过 160 字时截为反应者所在分句到 cue 分句），偏移相对正文，
   `draft_text[start:end] == text` 由测试保证。每个反应者最多 3 条，每章最多 30 条。

多个 payoff beat 时只跑一次检测器（全部 witnesses 一起参与归属），再把反应绑定到列出该 witness
的 beat；群像反应绑定到正文位置上最近的、在它之前出现的 payoff beat（beat 摘要用
`find_evidence_span` 定位，定位不到则落到第一个 payoff beat）。

## 工艺检查（全部 `warn`，check=`payoff_reaction`）

| category | 触发 | 文案要点 |
|---|---|---|
| `craft.payoff_no_reaction` | 本章有 payoff beat，正文没有任何非主角反应 | "爽点无反应：第2拍「…」之后没有任何非主角角色的可见反应（在场者 X/Y 没有可见反应）"；fix 点名在场者，窗口>0 时提示"也可在下一章开头用余波补上"；span 指向爽点所在句 |
| `craft.payoff_crowd_only` | 有 witnesses，但只有群像反应 | 点名缺席的在场者，span 为群像反应句 |
| `craft.payoff_reaction_deferred` | 窗口内第 p 章契约有爽点、其 current actuals 存在且（剔除主角后）0 条反应、重读 p 的正文也无反应，其间各章也没补上，且本章正文仍无这些在场者的反应 | "上一章爽点缺少反应，本章应补上余波反应：…" |

**"其间各章补上"怎么判定**：非爽点章的 actuals 不记录反应（enrich 只看本章契约的 payoff beat，
也不知道前一章的在场者），单看 actuals 永远了结不了义务。因此对 p < q < N 的每一章 q，
证据 = q 的 actuals 反应（剔除主角）∪ **用 p 的在场者（含别名）对 q 已提交的 L0 正文重跑检测器**；
p 本身也同样重读正文（带别名与主角名），与第 p 章当时的检查同口径。读的是 draft_index
最新修订（人工换稿后自动跟随）；内存库/文件缺失时退回只看 actuals。只在确有候选义务时才读文件
（窗口默认 1 时 p+1..N-1 为空，不读任何文件）。

- 没有 payoff beat 且没有待补余波 → 完全静默（新项目零新增 issue）。
- 只走 `craft_check` 扩展（不新增 skill，手动候选校验的调用顺序不变）。
- warn 不进 revise 循环，只在 `cfg.quality.enabled` 时参与润色触发（≥3 条 warn）。
- 治理接口 `/check/craft`（只有 draft/beats、无 plan_context）、`conn=None`、`ctx=object()` 均可安全运行。
- `payoff_binding`：打脸/羞辱/face-slap/humiliat*/comeuppance/turn the tables 类爽点没有 witnesses
  → `warn`（中英文案按 beat 文本语言），有则通过；单纯"反击"仍归 face_slap 但不要求在场者
  （交锋不必有观众，也避免老项目契约 witnesses 为空时每轮刷 warn）；其他爽点类型的 block 语义不变。

## 信息差机会（draft_brief ③）

as of 第 N-1 章，每个 (knower, secret_key) 取 `learned_chapter` 最大的一条边：

- 主角状态为 `knows`；本章出场者（契约 `must_include_entities`；无契约名单时用召回的活跃角色）
  状态为 `unaware` / `misinformed`；
- 契约名单里的名字一个都解析不到实体/别名时，同样退回召回的活跃角色；
- 跳过已公开的秘密（`public_from_chapter ≤ N-1` 或 `secrecy_level='public'`），
  以及契约 `must_not_reveal/must_not_resolve`、`forbidden_reveals` 关键词命中的秘密（不诱导提前揭示）；
- 同一秘密的多个目标合并成一行；排序：保密级别（top_secret 优先）→ misinformed 优先 →
  主角得知越早越优先；条数上限 `cfg.payoff.info_gap_suggestions`。

```
## 信息差（可用于当众揭晓/打脸）
- 信息差机会：王长老 还不知道「魔尊的真实身份」（主角已知）——可安排当众揭晓/打脸
```

提示块中不出现任何 FakeProvider 路由标记（测试逐个断言）。

## 配置（`cfg.payoff`，PayoffConfig）

| 字段 | 默认 | 作用 |
|---|---|---|
| `enabled` | True | 关闭后 brief 为空、检查静默、settle 返回 `{"skipped": "disabled"}`（actuals 回填不受影响，属事实记录） |
| `reaction_window_chapters` | 1 | 爽点章之后几章内可补反应；0 = 关闭"欠余波"检查与提醒 |
| `info_gap_suggestions` | 2 | 每章信息差机会条数上限；0 = 关闭 |

`craft_checks(ctx)` 的 ctx 不带 cfg，按序取：`ctx.extra["cfg"]`/`workspace["cfg"]`（调用方显式传入时）
→ `draft_brief` 每章写入同章黑板的 `workspace["payoff_reactions_cfg"]`（编排器管线，含其
`validate_final_candidate` 的 workspace 副本）→ `PayoffConfig()` 默认值。
- API 手动换稿（`app/api/orchestrator.py` 新建 validation_workspace）与治理 `/check/craft` 两条路径
  不经过 `draft_brief`，用默认值；`cfg.payoff` 没有项目级设置，那条路径自己的
  `build_project_config` 得到的也正是默认值，二者一致（测试断言了这一点）。若以后给 payoff 加项目级
  设置，需让这些路径把 cfg 放进 `ctx.extra["cfg"]`。
- 依赖：`build_draft_briefs` 必须在本章任何 craft_check 之前运行（当前在召回步骤），否则黑板副本缺失、
  退回默认值。

## 确定性与幂等

- 检测器、enrich、检查、settle 全部确定性；同一输入同一输出（无集合迭代序依赖，排序键明确）。
- `enrich_actuals` 以 (reactor, start, end) 去重，重复调用不重复追加。
- 所有 DB 与 L0 文件访问只读，`sqlite3.Error`/读文件失败一律降级；不新增表/列、不新增 LLM 调用。

## 局限

- 词表 + 句法启发式：长句、多重从句、隐性反应（"他沉默良久"）、只用代词且隔多句的指代可能漏判；
  "主角作主语 + 宾语后接 cue"除"向/朝/对…"外仍按宾语归属。只做 warn，宁漏勿误报。
- 不判断反应是否发生在爽点动作**之后**（beat 摘要定位不可靠）；铺垫段里的震惊也会被计入。
- `enrich_actuals` 无 DB：不知道主角名与别名，归属精度略低于 `craft_checks`；规划误把主角列为
  witness 时，**持久化的 actuals 里可能出现主角自己的反应**。所有读取方（欠余波判定、settle 报告）
  按 `meta_kv['protagonist']`/POV 卡解析主角后统一剔除，不会因此算作"已反应"。
- actuals 口径是**契约** beat：planner 临时生成的爽点（无锁定契约）不写入 actuals，"欠余波"检查
  也只看已落库的契约 beat；settle 报告对这种章节直接检测正文（`source="draft"`）。
- 评委的"反应与收获展示"维度、爽点循环完成率看板（23 号文档 P1-10）未在本特性实现。
