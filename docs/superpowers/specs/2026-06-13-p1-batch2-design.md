# P1 第二批设计：细纲契约 / 文风锚点 / 爽点循环完成率

日期：2026-06-13
来源：`docs/research_inkos_oh-story_20260612.md` P1 #7/#9/#10（oh-story §3.2.3/§3.2.4/§3.2.5）
前置：P1 后端核心三项已合入 main（a0a2d5c），本批是 P1 收尾。
范围：后端 + 前端最小集。用户已确认：钩子固定枚举+宽容归一；锚点仅手动录入；爽点完成率纯确定性口径。

## 0. 总原则

- 一次迁移 v13 覆盖全部 schema 变更（chapter_cards 加列 + style_anchors 新表），双路径（schema.sql 基线 + migrations 链）。
- LLM 输出一律宽容归一（沿用 P1#8 findings 的精神）：枚举匹配不上不报错，存 `other` 并退出相关检查。
- 确定性检查零 LLM 成本优先；评委只在已有调用里多拿一份 ground truth，不加新调用。
- 文风锚点缺失时 fail-fast 不瞎编：无匹配锚点 → 完全不注入，不降级到"随便选一段"。
- 前端最小集：Stats 页完成率徽章 + 逐章标记；章节卡新字段仅 API 透出（编辑 UI 留以后）。

## 1. #7 章节卡升级「细纲契约」

### 1.1 Schema（migration v13，chapter_cards 加 4 列）

```sql
ALTER TABLE chapter_cards ADD COLUMN target_emotion     TEXT;     -- 本章目标情绪词（如"紧张""扬眉吐气"）
ALTER TABLE chapter_cards ADD COLUMN opening_hook_type  TEXT;     -- 章首钩子（7 式枚举或 other）
ALTER TABLE chapter_cards ADD COLUMN hook_type          TEXT;     -- 章尾钩子（13 式枚举或 other）
ALTER TABLE chapter_cards ADD COLUMN expectation_score  INTEGER;  -- 期待度 1-5
```

schema.sql 基线同步加列（新库不走迁移）。

### 1.2 钩子枚举与归一（新文件 `novelforge/craft/hooks.py`）

- `OPENING_HOOK_TYPES`（7 式）：`suspense`(悬念) / `conflict`(冲突) / `dialogue`(对话切入) / `action`(动作) / `anomaly`(反常) / `crisis`(危机) / `flashback`(倒叙)。
- `ENDING_HOOK_TYPES`(13 式)：`cliffhanger`(命悬一线) / `reversal`(反转) / `reveal`(揭秘) / `new_threat`(新威胁) / `mystery`(新谜团) / `promise`(承诺/约战) / `arrival`(神秘人物登场) / `decision`(重大抉择) / `countdown`(倒计时) / `loss`(失去/代价) / `power_tease`(力量预告) / `relationship`(关系变化) / `humiliation`(受辱蓄势)。
- 每式带中文关键词表；`normalize_hook_type(raw, kind) -> str`：先精确匹配枚举 key，再关键词包含匹配中文描述，全不中返回 `"other"`。`other` 不参与同型检查。
- 枚举 key 用英文存库（CHECK 约束不加——LLM/人工输入面前 CHECK 太脆，归一函数是唯一入口）。

### 1.3 生产侧：volume_plan 输出契约扩展

`_SYSTEM` 输出 JSON 每章新增：`target_emotion`（一个情绪词）、`opening_hook_type`、`hook_type`（prompt 中列出两套枚举的中文名供选）、`expectation_score`（1-5，章尾钩子的期待度）。规则补充：**相邻两章 hook_type 不得相同**（prompt 软约束 + 下游确定性检查兜底）。

`_parse_plans` 归一两个 hook 字段、clamp expectation_score 到 1-5（非法→None）。API 落库 INSERT/UPDATE 带新列；`_load_chapter_card`、`ChapterCardModel`、`ChapterCardUpdateRequest` 同步加字段（PATCH 可人审改）。

### 1.4 消费侧：chapter_goal 注入 + 评委 ground truth

`assemble_chapter_goal`（chapter_suggest.py）在章节卡段追加一行（有则注入，无则跳过）：

```
本章细纲契约：目标情绪「紧张」；章首钩子=危机式；章尾钩子=反转式（期待度 4/5）
```

评委 `_JUDGE_SYSTEM` / `_SCORE_SYSTEM` 的 hook 维度措辞改为「章末钩子是否兑现本章目标中承诺的钩子类型与期待度（若有承诺）」——chapter_goal 已含承诺文本，**不改函数签名、不加调用**。

### 1.5 确定性检查：连续两章同型钩子

craft_check 新增第 8 项 `hook_repeat`（warn 级，零 LLM）：查 `chapter_cards` 本章与上一章的 `hook_type`，两者均非 NULL/other 且相同 → 报 `[craft.hook_repeat] 连续两章使用同型章尾钩子（X 式）`。任一缺失即跳过（规划层数据不全不误报）。

## 2. #9 文风锚点 few-shot

### 2.1 Schema（migration v13，新表）

```sql
CREATE TABLE IF NOT EXISTS style_anchors (
    id          TEXT PRIMARY KEY,
    emotion     TEXT NOT NULL,              -- 情绪标签（与 target_emotion 同词汇空间，自由文本）
    title       TEXT,                       -- 备注（出处等）
    content     TEXT NOT NULL,              -- 300-500 字范文段
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_style_anchors_emotion ON style_anchors(emotion);
```

### 2.2 API CRUD（`novelforge/app/api/craft.py` 扩展）

- `GET /v1/{project}/style-anchors`（?emotion= 过滤）
- `POST /v1/{project}/style-anchors`（emotion/title/content；content 长度 50-2000 校验）
- `PATCH /v1/{project}/style-anchors/{id}`（含 enabled 开关）
- `DELETE /v1/{project}/style-anchors/{id}`

### 2.3 选取与注入（orchestrator RECALL 阶段）

新函数 `craft/style_anchor.py: pick_style_anchors(conn, emotion, limit=2) -> list[dict]`：

- emotion 为空（本章无章节卡或卡上无 target_emotion）→ 返回 []。
- 先 `emotion` 精确匹配 enabled 锚点；无 → 字符 bigram Jaccard ≥0.5 的最近情绪标签（"紧张" vs "紧迫"）；再无 → 返回 []（**fail-fast，不退化为随机选段**）。
- 同情绪多段时取最新 2 段。

orchestrator 在 RECALL 后读本章 `chapter_cards.target_emotion`，调 pick 后写 `workspace["style_anchor_block"]`：

```
## 文风参考（仿其笔触与节奏，禁止照搬内容/人名/情节）
【参考段 1】（情绪：紧张）
…
```

**只由 chapter_draft_skill 消费**（planner/check 不需要，省 token）：user_msg 在 dynamic_context 之后、本章任务之前插入该块。位置在稳定前缀之后 → 跨章前缀缓存无损；同章多候选 user 消息字节一致 → 候选间缓存无损。`_polish`（润色）prompt 同样注入——润色是文风敏感环节。

## 3. #10 爽点循环完成率

### 3.1 口径（纯确定性，零 LLM、零落库）

某章「爽点闭环」= 该章存在任一确定性爽感证据：

| 证据源 | 判定（章号列均为 `change_chapter`，foreshadow_log 为 `chapter`） |
|---|---|
| `foreshadow_log` | 该章有 `action='payoff'` 行 |
| `character_power_log` | 该章有 `change_type IN ('breakthrough','unseal')` 行（正向实力变动；injury_drop/seal/init 不算爽点） |
| `item_log` | 该章有 `change_type IN ('acquire','craft')` 行 |

完成率 = 闭环章数 ÷ 已完成章数（stats series 的章集合）。阈值沿用报告：≥70% 高 / 50-70% 中 / <50% 低。

### 3.2 实现（`pipeline/stats` 现算）

三条 `SELECT DISTINCT <章号列> FROM …` 查询合成 set，与完成章集合求交；`ChapterStat` 加 `payoff_closed: bool`；`PipelineStats` 加 `payoff_loop_rate: Optional[float]`（无完成章时 None）。

### 3.3 前端（Stats.tsx）

- 汇总区加「爽点循环完成率」徽章：百分比 + 颜色（≥70% 绿 #15803d / 50-70% 黄 #b45309 / <50% 红 #b91c1c），tooltip 注明口径。
- 逐章表格/序列加一列小图标（✓ 闭环 / – 未闭环）。
- types.ts 同步 `payoff_closed` / `payoff_loop_rate`。

## 4. 不做什么（YAGNI）

- 不做拆文自动提取锚点（独立大功能，留以后）。
- 不做章首钩子的正文确定性检测（「前 100 字必须有钩子」需要 NLP 判定，误报率高；钩子约束通过细纲契约 prompt 注入 + 评委维度覆盖）。
- 不做爽点完成率落库/趋势预警，现算够用。
- 不做章节卡新字段的编辑 UI（API PATCH 已可改）。
- 三条既定 non-goal 不变（dedup 无稳定前缀 / 无 DOC·MCTS / 不替换 BM25）。

## 5. 测试策略（TDD）

`tests/test_p1_batch2.py`：

- **hooks**：枚举 key 精确匹配；中文关键词归一（"反转"→reversal）；不可识别→other。
- **hook_repeat**：相邻同型→warn；含 other/缺卡→不报；不同型→不报。
- **volume_plan 解析**：新字段归一与 clamp；prompt 契约含枚举标记。
- **style anchors**：精确情绪命中；近似情绪命中（bigram）；无情绪/无锚点→空（fail-fast）；draft prompt 含/不含锚点块；锚点块不进 planner prompt。
- **payoff_loop_rate**：三证据源各自触发闭环；无证据章不闭环；完成率计算与空集 None。
- **API**：style-anchors CRUD；chapter-cards PATCH 新字段；stats 透出新字段。

`tests/test_migrations.py`：v13 双路径（迁移链 ALTER + 新库基线）断言新列与新表。

分支 `feat/p1-batch2`，逐任务 TDD 提交，完成后本地 `--no-ff` 合 main（沿用既定偏好）。
