# PRD：英文创作与英文市场小说风格

> 状态：v0.9 待确认稿
> 日期：2026-07-21
> 产品：zi
> 范围：新建项目、故事规划、章节生成、质量校验、冷启动导入与 Studio 国际化

## 1. 摘要

zi 当前是一套以中文网文为默认前提的长篇创作引擎。现有新建项目流程虽然已有 `genre`、`target_audience`、`tone` 和文风锚点，但章节生成、候选择优、工艺检查、字数统计、检索分词与多数界面文案仍是中文网文口径。

本需求不是“增加 English 选项并翻译提示词”，而是建立三个彼此独立的产品维度：

1. **界面语言 `ui_locale`**：用户用什么语言操作 zi。
2. **作品语言 `content_language`**：模型最终用什么语言写小说。
3. **目标市场与写作配置 `target_market + writing_profile`**：作品遵循哪类读者预期、连载形态和叙事风格。

建议在新建项目流程中新增一个明确的 **“语言、市场与风格 / Language, Market & Style”** 环节，位于“基础信息”之后、“情节设定生成”之前。英文作品首先支持面向全球英文读者的 Web Serial；保留中文项目的现有行为，不自动翻译已有项目。

## 2. 背景与现状判断

### 2.1 当前已有能力

- 新建项目可选择中文网文题材，填写或生成 `premise`、`protagonist`、`main_conflict`、`target_audience`、`tone` 等设定。
- 设定散件写入项目数据库 `meta_kv`，力量阶梯写入 `power_ranks`。
- 故事方向、卷纲、章节路线和章节正文均可由 LLM 生成。
- 项目支持按情绪选取 `style_anchors` 作为 few-shot 文风参考。
- 章节生成后有一致性、节奏、爽点、钩子、人物声音和候选稿择优等检查。

### 2.2 不能仅改前端的原因

当前实现中存在以下中文假设：

- `ChapterDraftSkill` 明确要求“纯中文、≥1000 字”。
- `SettingGenSkill`、故事规划、卷规划、章节路线、工艺审稿等系统提示词均以中文网文为角色设定。
- 候选稿预筛按字符数判断，英文应按单词数及目标区间判断。
- `PacingController` 使用“每 3000 字小爽点、约 10 章大爽点”的单一中文网文节奏。
- `jieba`、中文 bigram、中文引号和中文钩子关键词影响检索及确定性检查。
- 冷启动抽取只声明处理“已有中文网文章节”。
- `target_audience` 与 `tone` 虽已落库并进入部分规划上下文，但没有形成贯穿起草、修订和评审的强约束。
- Studio、Agent 问题、错误提示和状态标签基本为中文。

因此，本需求需要同时覆盖产品数据、提示词编排、长度与质量规则、检索、测试和 UI 国际化。

## 3. 产品原则

1. **语言不等于市场**：英文作品可能面向美国、英国或全球英文读者；中文界面用户也可以写英文小说。
2. **题材不等于风格**：Fantasy 是题材，LitRPG/Progression Fantasy 是子类型或 trope，First Person/Past Tense 是叙事配置，Fast-paced 是节奏，必须分开建模。
3. **项目级明确选择，不靠 IP 或浏览器地区猜测**：浏览器语言只用于首次推荐，用户确认后写入项目。
4. **规则由写作配置驱动**：章节长度、节奏、POV、时态、拼写体系、内容边界和质量量表必须来自版本化 `writing_profile`。
5. **不模仿具体在世作者**：预设使用抽象、可解释的文体特征，不提供“写得像某作者”的官方选项。
6. **旧项目零回归**：已有项目迁移后默认 `zh-CN + cn_web_novel_v1`，行为与当前一致。

## 4. 目标与非目标

### 4.1 MVP 目标

- 用户可以在中文或英文界面中创建英文小说项目。
- 用户可以明确选择作品语言、英文拼写体系、目标市场、发布形态和一个主风格预设。
- 设定、规划、正文、修订、摘要、抽取和评审在同一项目内遵守所选英文配置。
- 英文章节使用单词数、英文标点、英文对话和英文可读性口径进行校验。
- 项目创建后可查看写作配置；修改配置只影响未来生成，并留有版本记录。
- 英文项目与中文项目可以同时存在，数据和缓存不串用。

### 4.2 非目标

- MVP 不自动翻译已有中文小说。
- MVP 不承诺一键发布到 Royal Road、Wattpad 或 KDP。
- MVP 不做各国法律意义上的完整本地化，也不处理税务、支付和版权登记。
- MVP 不训练或微调专属模型；优先通过版本化提示词、风格配置和评测集实现。
- MVP 不一次支持所有英语变体；先支持 `en-US`，数据结构预留 `en-GB`。

## 5. 用户与核心场景

### 5.1 用户画像

- **中文母语、写英文的网文作者**：需要英文自然度和市场风格辅助，但希望使用中文界面。
- **英文母语 Web Serial 作者**：需要英文界面、长篇一致性、滚动规划与稳定连载节奏。
- **双语作者/工作室**：需要同一 Studio 管理不同语言项目，并能复用结构化世界设定。

### 5.2 Jobs to be Done

- 当我开一本新书时，我希望先明确语言、市场和写作规则，后续每个生成环节都自动遵守。
- 当我不熟悉英文市场分类时，我希望看到可解释的风格预设、适用读者和结果预览，而不是术语堆砌。
- 当我修改风格时，我希望知道会影响哪些未来章节，不破坏已有正文和 canon。
- 当模型偏离 POV、时态、拼写或章节长度时，我希望系统能发现、解释并给出修订建议。

## 6. 新建项目流程

### 6.1 推荐流程

将当前单页弹窗改为三段式向导：

1. **基础信息 / Basics**
   - 书名（可空）
   - 作品语言 `content_language`
   - 界面语言仅在全局设置展示，不与作品语言绑定

2. **新增环节：语言、市场与风格 / Language, Market & Style**
   - 目标市场：`global_english`（MVP 默认）
   - 英文变体：`en-US`；预留 `en-GB`
   - 发布形态：`web_serial`（MVP 默认）、`novel`（P1）
   - 主类型 `primary_genre`
   - 子类型/主题标签 `story_tags[]`
   - 主风格预设 `style_preset_id`
   - POV、时态、节奏、文笔密度可使用预设值，也可展开高级设置覆盖
   - 内容分级与警告
   - 右侧展示 100–150 词的“效果说明/示例”，示例为系统自有文本

3. **情节设定 / Plot Setup**
   - 现有“自己填 / 系统生成选项”能力
   - 设定生成必须把第二步配置作为锁定上下文
   - 候选卡展示与目标风格相关的差异原因

### 6.2 跳过与默认值

- 用户可以跳过第二步的高级字段，但不能跳过 `content_language`。
- 选择英文后默认：`global_english + en-US + web_serial + accessible prose + third_person_limited + past_tense`。
- 用户返回上一步修改语言时，若已经生成设定候选，必须提示“语言变更会清空候选并重新生成”。
- 不用浏览器地区自动写入项目；只用于默认推荐，最终必须由用户确认。

### 6.3 项目创建后的编辑规则

- `content_language`：有正文前可直接修改；已有正文后需创建“语言迁移任务”，MVP 只提示不支持自动迁移。
- `target_market`、英文变体、风格预设：可修改，生成新的 `writing_profile_version`，仅影响后续生成。
- POV/时态：已有正文后修改为高风险操作，要求二次确认，并从下一章生效。
- 历史生成记录必须保存当时的 profile 版本，便于复现与回滚。

## 7. 英文风格信息架构

### 7.1 需要拆开的字段

| 维度 | 字段 | 示例 |
|---|---|---|
| 作品语言 | `content_language` | `en-US` |
| 目标市场 | `target_market` | `global_english` |
| 发布形态 | `publication_format` | `web_serial` |
| 主类型 | `primary_genre` | `fantasy` |
| 子类型/标签 | `story_tags[]` | `progression_fantasy`, `litrpg`, `cozy` |
| 读者分级 | `audience_rating` | `general`, `young_adult`, `adult` |
| 风格预设 | `style_preset_id` | `en_progression_serial_v1` |
| 叙事人称 | `pov` | `third_person_limited` |
| 时态 | `tense` | `past` |
| 节奏 | `pace` | `fast`, `balanced`, `slow_burn` |
| 文笔密度 | `prose_density` | `accessible`, `descriptive`, `literary` |
| 对话密度 | `dialogue_density` | `low`, `medium`, `high` |
| 章节长度 | `chapter_word_range` | `[1500, 2500]` |
| 内容警告 | `content_warnings[]` | `graphic_violence`, `profanity` |

主类型与 tags 必须分开。英文连载平台通常允许多个类型或更细标签；平台分类会影响检索和读者预期，不能继续用单一 `genre` 承载所有信息。

### 7.2 MVP 建议预设

| 预设 ID | 用户可见名称 | 核心特征 | 默认章节长度 |
|---|---|---|---|
| `en_progression_serial_v1` | Progression Fantasy Serial | 明确成长、阶段目标、频繁进展反馈、章尾推动 | 1,500–2,500 words |
| `en_litrpg_serial_v1` | LitRPG Serial | 可读数值反馈、升级闭环、系统信息不过载 | 1,500–2,500 words |
| `en_epic_fantasy_v1` | Epic Fantasy | 世界尺度、群像/主线平衡、较高描写密度 | 2,000–3,500 words |
| `en_cozy_fantasy_v1` | Cozy Fantasy | 低压冲突、关系与日常回报、温暖语调 | 1,500–3,000 words |
| `en_grimdark_v1` | Grimdark Fantasy | 高代价、道德灰度、克制而非无目的猎奇 | 1,800–3,000 words |
| `en_romantasy_v1` | Romantasy | 感情线与外部主线双推进、关系节拍明确 | 1,800–3,000 words |
| `en_mystery_thriller_v1` | Mystery / Thriller | 线索公平、信息控制、悬念升级、避免作弊式反转 | 1,500–2,500 words |
| `en_ya_speculative_v1` | YA Speculative | 清晰角色声音、身份/成长主题、适龄内容边界 | 1,500–2,500 words |

说明：章节长度应是可配置区间，而不是平台硬规则。Royal Road 官方资料倾向推荐稳定更新，并提到约 2,000 词是常见的舒适停点；因此 1,500–2,500 词适合作为英文 Web Serial 的 MVP 默认，而非强制标准。

### 7.3 风格预设内容结构

每个预设必须是版本化配置，而非散落在代码中的自然语言：

```json
{
  "id": "en_progression_serial_v1",
  "language": "en-US",
  "market": "global_english",
  "format": "web_serial",
  "prompt_rules": {
    "pov": "third_person_limited",
    "tense": "past",
    "pace": "fast",
    "prose_density": "accessible",
    "progress_feedback": "frequent",
    "chapter_end": "forward-driving hook"
  },
  "quality_rules": {
    "chapter_word_range": [1500, 2500],
    "max_pov_shifts": 0,
    "tense_consistency": true,
    "requires_progress_event": true
  },
  "prompt_version": "1.0.0"
}
```

## 8. 功能需求

### 8.1 P0：项目与目录

- 新增可版本化写作配置目录，后端为唯一事实源；前端从 API 读取，不复制枚举。
- 新建项目请求增加语言、市场、发布形态、风格配置和标签。
- 项目详情与列表返回可展示的语言和风格摘要。
- 项目设置页可查看当前 profile、版本、生效章节和修改历史。
- 旧项目读取时使用确定性默认配置，不要求用户立即补填。

### 8.2 P0：设定与规划

- `SettingGenSkill` 输入并锁定 `content_language`、`target_market`、`primary_genre`、`story_tags`、`writing_profile`。
- 英文项目所有候选字段值使用英文；结构化 key 保持英文稳定 ID。
- 故事方向、卷纲、章节路线、章节契约和 Agent 提问均按项目语言输出。
- 规划规则按 profile 替换：“爽点”不能作为所有英文类型的统一硬规则；例如 Cozy 应强调低压回报，Mystery 应强调公平线索，Romantasy 应强调双线推进。
- 任何 LLM JSON 输出继续做 schema 校验、重试和降级；降级文案也必须匹配项目语言。

### 8.3 P0：章节起草

- 将硬编码中文 `_SYSTEM` 改为按 `writing_profile` 编译的版本化系统提示词。
- 英文正文只允许输出目标英文变体；专有名词可按项目设定保留。
- 使用 `word_count` 而非 Python 字符数作为英文长度指标。
- 目标长度来自 profile；生成上限结合模型 token 能力动态计算。
- 稳定前缀缓存 key 必须包含 `content_language + profile_id + profile_version`，避免中英文或不同风格串缓存。
- 生成记录保存 `prompt_version`、`profile_version`、模型、token 使用量和成本。

### 8.4 P0：英文质量检查

新增或改造以下检查：

- `language_match`：正文主语言与项目语言一致。
- `word_count_range`：章节词数位于目标区间；允许软阈值与硬下限分开。
- `pov_consistency`：无意外 POV 跳转。
- `tense_consistency`：叙述时态稳定。
- `spelling_variant`：`en-US`/`en-GB` 拼写一致性。
- `dialogue_format`：英文引号、段落和 speaker 切换合理。
- `style_adherence`：是否遵守节奏、文笔密度、关系线/升级线/线索线等 profile 规则。
- `repetition`：句首、短语、角色口癖和 AI 常见模板化表达不过度重复。
- `readability`：仅作提示，不以单一可读性公式否决文学文本。
- `content_warning_match`：生成内容触及敏感项时提醒补充警告，不自动替用户发布。

候选稿选择不能再用“最长稿优先”作为英文兜底，应优先使用“满足长度区间 + 契约完成度 + profile 遵守度”，再以信息完整度裁决。

### 8.5 P0：召回、抽取和记忆

- 分词器按项目语言路由：中文继续 jieba；英文至少使用 Unicode/word tokenizer、大小写归一和基础词形归一。
- `tokenizer_version` 增加语言与实现版本；变更时支持按项目重建 FTS。
- Hash embedding 已支持 ASCII 词，可继续作为离线兜底，但英文评测集需单独验证召回率。
- 冷启动切章支持 `Chapter 1`、`Chapter One`、`Prologue`、`Epilogue` 和常见英文标题格式。
- `ColdExtractSkill` 根据项目语言选择抽取提示词，实体名、关系和证据保持原文语言。
- 摘要、actuals、偏航检测、伏笔与 canon 提案均使用项目语言，不把英文同义词误判为不同实体。

### 8.6 P0：文风锚点

- `style_anchors` 增加 `language`、`source_type`、`source_note`、`rights_confirmed`、`profile_tags`。
- 只召回与项目语言匹配的锚点；没有匹配时不随机跨语言注入。
- 官方预设示例必须为自有或可授权文本。
- 用户上传参考文本时提示确认其有权使用；官方产品不提供具体作者模仿入口。

### 8.7 P1：界面国际化

- 引入 i18n 资源层，至少支持 `zh-CN`、`en-US`。
- 导航、表单、空状态、错误、进度事件、审核队列、Agent 选择题与 ARIA 标签全部资源化。
- 日期、数字、词数、字符数和价格按 locale 格式化。
- 后端机器错误返回稳定 `error.code + params`；前端本地化文案，避免后端只返回中文句子。
- `ui_locale` 存在本地偏好层；未来有账号后再同步到账户设置。

> 若本次目标是“先让中文用户写英文小说”，界面全量英文化可降为 P1；但作品生成链路的英文支持仍是 P0。

## 9. 数据模型建议

### 9.1 项目级字段

MVP 可继续存入 `meta_kv`，避免立刻扩大 `projects.json` 的兼容风险：

- `content_language`: `zh-CN | en-US | en-GB`
- `target_market`: `cn_web | global_english | us | uk`
- `publication_format`: `web_serial | novel`
- `primary_genre`: 稳定英文 ID
- `story_tags_json`: JSON array
- `audience_rating`
- `content_warnings_json`
- `writing_profile_id`
- `writing_profile_version`
- `writing_profile_overrides_json`
- `writing_profile_effective_from_chapter`

中期应建立 append-only `writing_profile_versions` 表，记录：

- profile 快照
- 修改人/来源
- 生效章节
- 创建时间
- prompt 版本
- 变更原因

### 9.2 API 变更

新增：

- `GET /v1/catalogs/writing-profiles?language=en-US&format=web_serial`
- `GET /v1/catalogs/genres?language=en-US&market=global_english`
- `GET /v1/{project_id}/writing-profile`
- `PATCH /v1/{project_id}/writing-profile`

扩展：

- `POST /v1/projects`
- `POST /v1/projects/setup/suggest`
- 项目 list/get response
- 章节生成、冷启动导入和规划响应的可观测元数据

兼容规则：老客户端不传新字段时，后端按 `zh-CN + cn_web + cn_web_novel_v1` 创建；新客户端不自行猜后端默认。

## 10. 提示词与 AI 工程要求

- 提示词从代码内常量迁移为按 Skill、语言、profile、版本管理的模板。
- system、用户输入、世界设定和动态召回保持角色分离；用户文本需长度限制和注入清洗。
- 结构化输出全部走 Pydantic/JSON schema 校验，失败策略为：同档重试 → 有条件升档 → 语言匹配的确定性降级。
- 建立英文黄金评测集，覆盖设定、规划、章节、抽取、摘要和审稿；每次 prompt 变更跑回归。
- 记录每阶段 input/output token、延迟、重试、降级、profile 遵守得分和单位章节成本。
- 英文长章节需显示阶段进度或流式状态，避免长时间无反馈；结构化结果在完整校验后落库。
- 不把英文预设和大段样例重复塞入每次动态上下文；稳定 profile 进入可缓存前缀。

## 11. 安全、版权与平台适配

- 内容警告与受众分级需要显式选择，不能从“英文区”推断。
- 参考文风采用可解释特征，不支持复制特定作者的独特表达。
- 冷启动导入和参考文本保留来源信息与权利确认状态。
- 导出到外部平台前，未来应提供平台规则检查，但 MVP 只展示提醒。
- Royal Road 官方目前要求准确的 genre/tags 和必要内容警告，并区分 AI-Assisted 与 AI-Generated；若未来提供发布辅助，应把 AI 使用披露作为导出元数据，而不是默认隐藏。
- 不把不同英文市场的敏感内容、拼写与分类差异硬编码成一个永久规则，均通过可版本化 catalog/profile 管理。

## 12. 迁移与兼容

### 12.1 老项目

- 首次读取时补默认值，不重写正文、不重建 canon。
- `genre` 保持原值；新 `primary_genre` 可为空或映射到兼容值。
- 只有需要英文检索或 tokenizer 版本变化的项目才触发索引重建。
- 已保存的生成记录保持原 prompt/profile 信息为空，UI 展示“legacy”。

### 12.2 已有英文内容但项目无语言字段

- 不自动以抽样结果改写项目配置。
- 设置页可提供“检测建议”，展示置信度并由用户确认。
- 用户确认后从指定章节开始启用英文 profile；旧章节只用于召回和一致性，不自动改写。

## 13. 指标与埋点

### 13.1 北极星指标

- 英文项目完成首章且通过人工采用/保存的比例。

### 13.2 核心指标

- 新增环节完成率、退出率、平均耗时。
- 各风格预设选择率与后续修改率。
- 英文设定候选采用率、单字段 reroll 次数。
- 首次生成语言匹配率、POV/时态/长度一次通过率。
- 候选稿人工改选率和人工编辑距离。
- 每章生成延迟、token、成本、重试和降级率。
- 英文检索 Recall@K、canon 抽取准确率、同义实体重复率。
- 中文项目关键指标不得因本需求显著下降。

### 13.3 事件建议

- `project_setup_language_selected`
- `project_setup_style_selected`
- `project_setup_style_customized`
- `project_setup_step_completed`
- `writing_profile_changed`
- `generation_language_mismatch`
- `style_validation_failed`
- `english_chapter_accepted`

事件不得记录正文、私密设定或完整提示词，只记录 ID、版本和数值指标。

## 14. 验收标准

### 14.1 新建项目

- 用户可分别选择 UI 语言和作品语言，互不强制联动。
- 选择英文后只展示兼容的英文市场风格，选择中文时现有中文流程不变。
- 新增环节的默认值可直接继续，返回修改语言时能正确清理不兼容候选。
- 创建结果可从 API 与设置页完整回读，不依赖前端本地状态。

### 14.2 生成链路

- 一个 `en-US + en_progression_serial_v1` 项目从设定到首章的所有用户可见创作结果均为英文。
- 章节满足配置的硬下限，使用单词数展示；不会把 1,000 个英文字符误判为合格章节。
- 英文正文违反 POV、时态或拼写变体时，系统返回可定位的 finding。
- 更换 profile 后，旧章节和历史生成记录不变，新章节记录新版本。
- 中英文两个项目并行生成时，prompt cache、style anchor 和召回结果不串用。

### 14.3 可靠性

- LLM 返回错误语言、畸形 JSON 或空结果时不会把脏数据写入项目。
- 无 API key 时返回对应语言的示例/降级提示，哨兵数据不能落库。
- 英文功能关闭或回滚时，中文项目无需数据回滚即可继续使用。

## 15. 测试计划

### 15.1 单元测试

- 新字段 schema、默认值和旧请求兼容。
- profile catalog 过滤、覆盖和版本比较。
- 英文 word count、POV、时态、拼写、对话与语言检测。
- 中英文 tokenizer 路由、英文标题切章。
- style anchor 语言隔离和 rights 字段。
- prompt cache key 包含语言/profile 版本。

### 15.2 集成测试

- 中文 UI → 创建英文项目 → 生成英文设定 → 规划 → 首章 → 检查 → 保存。
- 英文 UI → 创建中文项目，验证 UI locale 与内容语言解耦。
- 旧项目迁移后继续生成中文章节。
- profile 从 v1 切到 v2，仅影响指定章节之后。
- 英文冷启动整本导入、切章、抽取、去重与审核。

### 15.3 AI 回归评测

每个 MVP 风格至少 10 组固定输入，检查：

- 语言纯度与拼写变体。
- profile 特征遵守度。
- 结构化输出有效率。
- 角色与世界状态一致性。
- 章节契约完成度。
- 幻觉设定、跨语言污染和具体作者模仿风险。

上线门槛建议：核心结构化输出有效率 ≥99%，语言匹配率 ≥98%，中文基线无统计显著回退。

## 16. 分阶段交付

### Phase 0：定义与评测

- 确认目标英文市场、发布形态和首批风格。
- 固化 taxonomy、profile schema、英文评测集和老项目默认策略。

### Phase 1：英文创作闭环（P0）

- 新建项目新增环节。
- 数据/API/profile catalog。
- 设定、规划、起草、抽取和英文核心校验。
- 词数、tokenizer、缓存隔离、迁移与测试。
- UI 可先保持中文，但可创建和管理英文作品。

### Phase 2：英文用户体验（P1）

- Studio 全量 `en-US` 国际化。
- 本地化错误、Agent 问题、帮助和无障碍文案。
- 英文项目设置与 profile 历史。

### Phase 3：市场深化（P2）

- `en-GB`、更多 genre/tags、平台导出校验。
- 平台元数据、AI 披露提醒、内容警告映射。
- 基于数据优化各 profile，而不是把所有英文作品归为单一风格。

## 17. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 把“英文”当一种风格 | 输出同质、市场不匹配 | 语言/市场/题材/风格分层建模 |
| 只翻译提示词 | 中文爽点与节奏规则继续误伤英文类型 | profile 驱动规划与工艺规则 |
| UI locale 与作品语言绑定 | 中文作者无法方便写英文，英文作者无法管理双语项目 | 两个独立字段 |
| 英文按字符数验收 | 章节过短仍通过 | 语言感知 word count |
| profile 修改污染旧章节 | 无法复现、角色声线突变 | append-only profile 版本与生效章节 |
| 具体作者模仿 | 版权与信任风险 | 抽象特征预设、来源和权利确认 |
| 风格样例扩大 prompt 成本 | 单章成本与延迟上升 | 稳定前缀缓存、短样例、版本监控 |
| 英文 UI 范围失控 | 延误创作闭环 | 创作链路 P0、全 UI i18n P1 |
| 平台规则变化 | 导出建议过时 | catalog 版本化，不把规则写死在 prompt |

## 18. 待产品确认

以下决定会显著改变范围，进入实施规格前必须确认：

1. **本期是否必须让英文母语用户使用完整英文 Studio？** 还是先满足“中文界面生成英文小说”，全 UI 国际化放下一期？
2. **首要发布场景是什么？** 建议先做英文 Web Serial；若首要目标是 Kindle/传统完整长篇，章节长度、节奏和风格预设需要换一套。
3. **“英文区小说风格”指市场预设，还是你已有一份明确风格清单？** 若已有，请提供名称、示例和优先级；建议不要使用作者姓名作为预设。
4. **目标英文变体与市场是否先定为 `en-US + global English`？** 如果第一批用户主要在英国或其他地区，应把拼写与内容提示提前纳入 P0。
5. **新增环节是否就是新建项目中的独立一步？** 本稿建议放在“基础信息”与“情节设定”之间，并允许项目创建后从下一章起修改。
6. **是否需要支持现有中文项目转为英文？** 本稿默认不自动翻译，只支持新英文项目和已有英文稿导入。

## 19. 外部依据

- [Royal Road Genres & Tags](https://www.royalroad.com/support/knowledgebase/98)：genre 与更细粒度 tags 分开，包含 LitRPG、Grimdark、Cozy、Progression 等英文连载常见分类，支持本 PRD 将“题材、标签、风格”拆分。
- [Royal Road Managing Your Fiction](https://www.royalroad.com/support/knowledgebase/83)：项目具有 Primary Language；官方更强调稳定发布，并把约 2,000 词描述为常见舒适章节长度，而非硬标准。
- [Royal Road Content Guidelines](https://www.royalroad.com/support/knowledgebase/114)：要求内容警告，并区分 AI-Assisted 与 AI-Generated，支持把内容警告和未来平台披露纳入产品设计。
- [Amazon KDP Categories](https://kdp.amazon.com/en_US/help/topic/G200652170)：市场、受众、分类和关键词共同影响作品发现，说明不能只依赖一个 `genre` 字段。
