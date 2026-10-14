# F4 评委去偏：正反两序判优 + 分维度质量门控

> 依据：调研 23 §七 P0-4。LongJudgeBench（EMNLP 2026）：创意写作两两判优准确率仅
> 0.508–0.639，交换顺序后判断不一致率 10.6%–78.7%（DeepSeek-V4-Flash 62%）；
> WSE-bench：单一加权总分不能代表质量。结论：评委必须正反两序比较，单一总分不宜做硬门禁。

## 1. 做了什么

| 部分 | 位置 | 行为 |
|---|---|---|
| 多候选评委两序判优 | `craft/candidate_judge.py::select_best` / `_judge_finalists` | 第 5 级评委正序、逆序各判一次，胜者一致才采纳，分数取两次均值 |
| 分维度质量门 | 新 `craft/quality_gate.py::evaluate_quality` | 任一维度低于下限即低质量；总分只作展示 |
| 润色门控 | `Orchestrator._quality_pass` | 用质量门代替 `score < min_score`；保留"craft warn ≥3 也润色" |
| autopilot 降级计数 | `app/autopilot_manager.py` | 低质量判定改用 `evaluate_quality`（两行替换） |
| 看板 | `app/api/orchestrator.py::pipeline_stats` + Studio 质量趋势 | `low_quality_count` 按质量门复算；新增评委不一致率 |
| 决策评委（AI 代选） | `planning/generation.py::recommend_decision_option` | 同样正反两序，不一致回落确定性排序 |

## 2. 两序判优（候选择优）

数据流：`_gen_candidate×N` → 确定性漏斗 1–4（预筛 / 硬校验 / 计划硬冲突 / 契约契合度）
→ 剩余 ≥2 个终选 → `_judge_finalists`：

1. 正序 `finalists` 判一次（提示里编号 `### 候选 0..k-1`）。
2. `judge_both_orders=True`（默认，`cfg.candidates.judge_both_orders`）时逆序再判一次，
   逆序结论按 `order[pos]` 映射回候选原下标。
3. 判定：
   - 两次胜者一致 → 采纳；`scores[i]` = 两次分数均值（单侧缺分取另一侧，夹到 0–10），
     `judge_disagreement=False`，`reason` 仍为评委理由（自由文本）。
   - 胜者不一致，或逆序结论无法解析 → 不采纳，`judge_disagreement=True`，
     `reason="judge_order_disagreement"`，`scores` 留空（不采信不一致评委的分数）。
   - 正序结论无效 → **不再发逆序调用**（单侧结论无法校验，多花一次也不会被采纳），
     走旧的 `judge_unavailable_*` 兜底，`judge_disagreement=None`。
   - 任一侧调用异常（网络、预算熔断）→ 视为"评委不可用"，`judge_disagreement=None`，
     不计入不一致率（`judge_verdicts[i].error` 记录异常类型）。
4. 确定性兜底：
   - 中文：`plan_fidelity` → `contract_score` → 正文长度（`max` 取首个，稳定）。
     漏斗第 4 级已按 plan_fidelity 收敛，因此通常等价于旧的"取最长稿"。
   - 英文：保持 `min(finalists)`（profile fit 已并列，稳定下标，不回到"最长稿"）。
5. 只有 1 个终选时不发任何 LLM 调用（漏斗各级提前返回）。

报告新增字段（随 `candidate_report` 平铺进 `pipeline_run.detail_json`）：

- `judge_disagreement`: `None`（未做两序校验）/ `False` / `True`
- `judge_verdicts`: `[{order, winner, scores, reason, valid[, error]}]`，
  `winner`/`scores` 已映射回候选原下标，`scores` 与 `report["scores"]` 同形。

`candidates` 阶段事件新增 `judge_disagreement`。

已知局限：3 个终选时简单倒序会让中间候选保持在中间位置，"中间位"偏差测不出来；
LongJudgeBench 报告的主要是首/末位偏差，因此按规格采用倒序（`test_m3`/`test_m6` 的固定
`winner:1` 夹具在倒序下仍一致）。

## 3. 分维度质量门

`evaluate_quality(score, dimensions, qcfg) -> QualityVerdict(low, reasons, mode)`，纯函数：

| `qcfg.gate_mode` | 判定 | `mode` |
|---|---|---|
| `"dimensions"`（默认） | 任一"有下限且有分"的维度 `< dimension_min[dim]` → 低 | `dimensions` |
| `"dimensions"` 但无可校验维度 | 回落 `score < min_score` | `total_fallback` |
| `"total"` | 旧口径 `score < min_score` | `total` |
| 未知取值 | 按 `"total"` 处理（配置拼错不静默放行） | `total` |
| 无总分也无维度 | 不判低 | `unscored` |

- `reasons` 形如 `"hook 4<5"`、`"score 5.5<6"`，语言无关。
- 配置：`cfg.quality.gate_mode`、`cfg.quality.dimension_min`（默认 hook/pacing/character/prose
  各 5.0）、`cfg.quality.min_score`（6.0，回落用）。
- `verdict_detail(verdict, qcfg)` 把结论连同当时阈值落库：`detail_json["quality_gate"] =
  {low, reasons, mode, gate_mode, min_score, dimension_min}`；该 dict 本身可再作 `qcfg`
  传入 `evaluate_quality`，看板/换稿据此复算。

### 3.1 `_quality_pass`

1. 取分：
   - 单稿：`score_chapter_detailed`（与旧行为相同，一次调用）。
   - 多候选 + `gate_mode="total"`：复用评委两序均值分（省一次调用，同旧行为）。
   - 多候选 + `gate_mode="dimensions"`：评委不给维度，对终稿（已 revise）调用一次
     `score_chapter_detailed` 取总分 + 维度；评分失败时回落评委分。
2. `need_polish = polish_enabled and 无 hard block and (verdict.low or craft warn ≥ 3)`。
3. 润色稿保留规则 `is_improvement`：未过线项更少者胜；持平时总分不降即保留。
   纯总分口径下等价于旧规则 `new >= old`；维度口径下"修好了低分维度、总分略降"的
   润色稿不再被误回退。保留润色稿时维度分与正文同源（新评分无维度则清掉旧维度）。
4. 润色中途异常（预算熔断等）一律回退原稿与原 craft 问题。
5. 写 `workspace["quality_gate"]` → `detail_json["quality_gate"]`；`quality` 阶段事件带 `gate`。

### 3.2 autopilot

`low_quality = cfg.quality.enabled and evaluate_quality(outcome.quality_score,
outcome.quality_dimensions, cfg.quality).low`，其余降级/暂停逻辑不变。

### 3.3 看板（`GET /{pid}/pipeline/stats`）

- 每章取最新 completed run，读取 `detail_json`：
  - 质量门按该 run 落库的阈值复算（无 `quality_gate` 的旧数据用默认 `QualityConfig`，
    无维度时即旧口径 `score < 6.0`）；人工换稿刷新分数/维度后结论随之更新。
  - `ChapterStat` 新增 `quality_low`（None=未评分）、`quality_gate_reasons`、`judge_disagreement`。
- `PipelineStats` 新增 `judge_runs`（做过两序校验的章数）、`judge_disagreements`、
  `judge_disagreement_rate`（无两序校验时为 null）；`low_quality_count` 改为未过质量门的章数。
- 人工换稿（select-candidate）在重新评分后同步刷新 `detail_json["quality_gate"]`。
- 前端：质量趋势标题行显示"⚖ 评委不一致 N%"（≤20% 绿、≤40% 黄、其余红），低质量提示改为
  "N 章未过质量门"；柱状图按 `quality_low` 着色，悬浮提示列出未过线项与两序不一致标记。

## 4. 决策评委（AI 代选）

`recommend_decision_option(..., judge_both_orders=True)`：选项按原序、倒序各判一次
（每次都把 `option_index` 重新编号为展示位置，答案再映射回原下标）。两次一致 →
`source="llm_judge"`，`judge_disagreement=False`；不一致（或倒序结论无效）→
`_fallback_recommendation`（按选项分数/推荐顺序），元数据带 `judge_disagreement=True`
与 `judge_verdicts`，理由中英双语。只有 1 个选项时不做第二次调用。FakeProvider 环境
（`NOVELFORGE_PROVIDER=fake`）与无模型配置时仍直接确定性回落，零调用。

## 5. 确定性 vs LLM

- 确定性：漏斗 1–4、两序一致性判定与分数均值、兜底排序、质量门、润色保留规则、看板统计。
- LLM：评委（每次择优 ≤2 次，MID 档）、`score_chapter_detailed`（维度门控下多候选路径
  多 1 次）、润色与复评（与旧行为相同）、决策评委（FAST 起、≤2 次）。

## 6. 成本

- 多候选章：评委调用 1 → 2 次；维度门控 + 质量检查开启时再加 1 次单稿评分。
- 单稿章：无新增调用。

## 7. 局限

- 倒序对 3 个终选的中间位置不敏感（见 §2）。
- 评委仍只看每个候选前 2500 字、评分只看前 4000 字，章末钩子常不在视野内（沿用旧限制）。
- 维度下限是全局默认值，未按平台/题材档位细分；`QualityConfig` 仍未从项目设置加载，
  看板对旧数据使用默认阈值复算。
- 不一致率只统计"每章最新 completed run"，同章多次重跑的历史不计入。
