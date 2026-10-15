# F3 极性序列与抑扬上限（v22）

> 依据：调研 23 §七 P0-3。千幻冰云"最惨不能惨过三章，最爽也不能爽过三章"；Story Grid
> "每场必须转、避免同向极性链"；猫腻"小高潮、平复期和余韵。高潮越小，平复期越短"。

## 做了什么

每章记录**实际**转向（`+` 趋近主角目标 / `-` 远离目标 / `±` 得失参半）与转折类型
（`action` / `revelation`），按逐章历史维护连续低谷、同向连续计数，并在下一章的
规划与起草前给出"需要转机 / 反向转折 / 安排余韵"的要求；起草后的工艺检查对计划
不满足上限的章节给出 warn。全部确定性，**不增加任何 LLM 调用**。

| 位置 | 内容 |
|---|---|
| `craft/polarity.py` | `enrich_actuals` / `settle` / `craft_checks` / `draft_brief`（扩展点见 `craft/extensions.py`） |
| `craft/pacing.py` | `PacingState` 读新列 + `needs_turn`；`aftermath` 计为冷却；`recompute_polarity_streaks`；中文转机提示 |
| `control_plane/orchestrator.py` | `_profile_pacing_hint`：英文转机提示；recall 步骤 `get_state` 传入 `cfg.polarity.max_low_streak` |
| `app/api/craft.py` + `app/models.py` | `GET /v1/{project_id}/pacing`（游标 + 最近 20 章） |

## 数据流

```
recall 步骤 ── pacing.get_state(max_low_streak=cfg) ──► 节奏提示（chapter_goal，中/英）
            └─ draft_brief ───────► workspace["craft_brief"]（契约之后，稳定前缀之外）
                                    + workspace["polarity_settings"]（cfg 通道）
PLAN / DRAFT ── planner 与 drafter 都渲染 craft_brief
CHECK ×N ───── craft_checks：读 pacing_state(章 < N) + 本章计划转向 → warn
COMMIT ─────── extract_chapter_actuals → enrich_actuals → chapter_actuals.payload_json
SETTLE ─────── pacing.update（旧计数，余韵清零高潮连击）
               └─ ext_polarity.settle：upsert pacing_state(chapter) → 重算 pacing_cursor
```

## 实际转向的证据顺序（enrich_actuals / settle）

1. **计划已执行**：契约有 `polarity`，且契约 beats 在终稿中完成 ≥ 一半
   （`actuals.completed_beats`）→ 采用契约 polarity；turning_point 取契约值，缺省时由 beats 推断。
2. **价值轴推断**：已完成 beats 的 `value_axis`：
   - `A→B` / `A->B` / `从A到B` / `from A to B`：比较终态与起态的情感值（终态得失参半 → `±`）；
   - `轴名：A→B` / `axis: A -> B`：先拆出轴名，轴名不参与起终态比较（否则"实力：弱→强"里的
     价值物"实力"会让起态得 +、被误算成 `-`）；无冒号的"实力弱→强"/"status low -> high"
     （一侧只有量级词）同样把价值词当轴名；
   - 两态情感持平（或都无情感词）时比较量级词（高/低、强/弱、多/少、more/less…），好坏由
     轴名或两态共享的价值词决定："地位：低→高" = `+`，"危险：低→高" = `-`，
     "危机→更大的危机" = `-`；轴名是对手的价值物时取反（"敌人实力：弱→强" = `-`）；
     没有轴名的纯量级变化（"弱→强"、"low -> high"）不猜；两态都只是量级词且有轴名时由轴名定好坏
     （"pressure: weak -> strong" = `-`）；英文 weak/strong 本身也是情感词，裸写 "weak -> strong" = `+`，
     且只由量级词构成的一侧不会被当成轴名；
   - `X↑ / X↓ / X上升 / trust down`：情感值 × 方向（"权力↓" = `-`，"危机↑" = `-`，
     "对手实力↓" = `+`）；
   - 只有轴名（"power"、"生死"）或中性状态（"悬念↑"）→ 不猜。
   - 每个 beat 得 + / - / ±，hook 不计（章末悬念是下一章的起因）；最后一个有方向的 beat
     （本章高潮）权重 ×2：净值 >0 → `+`，<0 → `-`，持平或高潮本身为 ± → `±`。
     例：`[-, +]` 抑后扬 → `+`；`[-, -, +]` 久压小胜 → `±`；`[+, -]` 乐极生悲 → `-`。
3. **未知**：保持 `None`。未知转向会打断连续计数（宁可少报，不误报）。

actuals 未给出转向时（无契约 beats；或契约未锁定、planner 另写了 beats），`settle` 用
本章实际使用的 beats，经 `find_evidence_span` 在终稿中确认完成后重做第 2 步
（`source="beats"`）。锁定契约时两份 beats 相同，结论与 enrich 一致。

词典：中英各约 100 词（胜利/突破/认可/敬畏… vs 危机/受辱/失去/绝境…；win/trust/safe…
vs crisis/trapped/betrayed…），含"价值物"（权力/地位/power/status：越多越好）、
得失参半标记（惨胜/代价/bittersweet/pyrrhic）、紧邻否定（"失去了希望"）与化解
（"化解危机"、"escaped danger"）。只看紧邻词，避免"无敌的希望"这类误判。

turning_point 推断：在最后一个 turn/payoff_beat/tension_point beat 的摘要里找
揭示词（发现/真相/识破/learns/reveals…）→ `revelation`，否则行动词（击败/出手/
defeats/chooses…）→ `action`，都没有 → `None`。

## 存储

- `pacing_state`（每章一行，`chapter UNIQUE`，ON CONFLICT 更新 → 幂等）：
  `tension_level` 1 平缓（setup/aftermath/hook）· 2 有 turn · 3 有 tension_point ·
  4 有 payoff_beat（大爽点）· 5 payoff_beat 叠加其他高张力 beat（大高潮）；
  `pace` slow/steady/fast/climax/climax；`polarity`、`turning_point`；
  `notes` = JSON `{source, beat_types, aftermath, climax}`。
- `pacing_cursor.low_streak / polarity_streak / last_polarity`：每次结算后按
  `pacing_state` 从最新一章向前**重算**（章号须连续、转向须已知），不做累加，
  重写/换稿后重复结算结果不变。`pacing.update` 的 `_upsert_cursor` 只写旧列，冲突更新
  不会覆盖这三列。
- 结算写入包在 SAVEPOINT 里；未迁移到 v22 的库返回 `{"skipped": "schema"}`，不标降级。
- 报告进入 `pipeline_run.detail_json["extensions"]["polarity"]`：
  `{chapter, polarity, turning_point, source, tension_level, pace, low_streak, polarity_streak, last_polarity}`。

## 检查（craft_checks，全部 warn，中英双语）

"历史"= `pacing_state` 中本章之前、从 N-1 起章号连续的实际转向。
"计划转向"= 契约 `polarity`（orchestrator / 换稿验证从 `plan_context` 取；治理端点从
`chapter_plan_versions.payload_json` 取），契约未定时用本章 beats 的价值轴推断。

| check | 条件 | repair_scope |
|---|---|---|
| `polarity_turn` | 连续低谷 ≥ `max_low_streak` 且计划仍为 `-` 或未定 | structural |
| `polarity_streak` | 计划转向与末尾同向链相同，且链长 ≥ `max_same_polarity_streak`（`-` 已由上一条报过则不重复） | structural |
| `aftermath` | 上一章是大高潮（张力 5；无结算记录时看其计划 beats）且其最后一个 payoff_beat 之后没有 setup/aftermath；本章有 beats、无 aftermath、首个 beat 也不是 setup/aftermath | local |

新项目或无历史时零告警；旧库/缺表时静默。

## 提示块（draft_brief）

仅在相关时输出（中文 `## 节奏要求`，英文 `## Pacing requirements`），不含任何
FakeProvider 路由标记：

- 连续低谷：契约未定 → "本章必须安排转机（+ 或 ±）"（游标提示已写明时省略，避免重复）；
  契约仍为 `-` → "至少给出一线转机，让本章成为 ±"；契约已是 `+`/`±` → 不输出。
- 连续顺境：契约未定 → "本章需反向转折（受挫/代价/新危机）"；契约仍为 `+` → "为胜利附上
  代价或新隐患"。
- 上一章大高潮（口径同 `aftermath` 检查）且契约 beats 无 aftermath → 开头安排余韵；
  上一章 2 个高张力 beat 建议"一小段"，≥3 个建议"约前三分之一"（高潮越大平复越长）。
  单个 payoff_beat（张力 4）是小爽点——"高潮越小，平复期越短"——不提示，避免爽文
  几乎每章都被要求开场平复、开头同质化。

游标提示（`recommend_beat_hint` 中文 / `_profile_pacing_hint` 英文）在 `needs_turn`
（`low_streak ≥ max_low_streak`，阈值取项目配置）时把转机要求放在最前，其余节拍建议照旧。
只有游标阈值与项目阈值一致时，提示块才省略重复的转机要求。

## 配置（`cfg.polarity`，PolarityConfig）

| 字段 | 默认 | 作用 |
|---|---|---|
| `enabled` | True | False 时：不写 pacing_state，三个计数清零，检查与提示块静默 |
| `max_same_polarity_streak` | 3 | 同向连续上限 |
| `max_low_streak` | 3 | 连续低谷达到即要求转机（`PacingState.needs_turn` 默认阈值同此） |

`craft_checks` 拿不到 cfg：`draft_brief` 在 recall 步骤把设置存进
`workspace["polarity_settings"]`，缺省时用默认值（治理端点 / 手动换稿验证）。

## API

`GET /v1/{project_id}/pacing`（阈值经 `build_project_config` 取项目实际配置）→
`{cursor: {…旧计数, low_streak, polarity_streak,
last_polarity, needs_turn, needs_cooldown, …}, chapters: [最近 20 章，升序，
{chapter, tension_level, pace, polarity, turning_point, detail}], max_same_polarity_streak,
max_low_streak}`。

## 局限

- 词典推断是启发式：只认带方向的价值轴，复杂反讽、长句否定、后置否定（"hope lost"）
  会判成 `±` 或 `None`；不读正文情感。
- 手动换稿验证（`app/api/orchestrator.py` 的 select-candidate）不跑 recall，`craft_checks`
  拿到的是默认阈值；该处 `get_state` 也未传阈值（非 F3 文件）。
- 价值轴默认以主角视角书写；"敌人：嚣张→溃败"这类以对手为主语的状态描述仍按字面情感计。
- 游标表示"截至最新已结算章"：重写较早章节时，下一章的游标提示可能滞后一章；
  提示块与检查按"本章之前的历史"计算，不受影响。
- 手动换稿（select-candidate）会重算并持久化 actuals（含新转向），但不跑结算，
  `pacing_state` 仍是旧稿的转向，直到该章重新生成。
- 只对 `+`/`-` 链报警；连续多章 `±` 不告警。
