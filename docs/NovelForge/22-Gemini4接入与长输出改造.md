# 22 · Gemini 4 接入与长输出改造

> 目标：把底层创作模型切到 Google Gemini 4（`gemini-4-argon`），并按其能力调整生成链路。
> 调研时间：2026-10（Gemini 4 Argon 于 2026-10-13 GA）。

## 1. 模型能力摘要

| 能力 | Gemini 4 Argon | 对本项目的含义 |
|---|---|---|
| 模型 ID | `gemini-4-argon`（Gemini API 付费层 / Vertex） | 三档 tier 默认都指向它 |
| 上下文 | 输入 + 输出**共享** 1,048,576 token | 输入越长，可用输出越少；provider 按估算输入收紧 `maxOutputTokens` |
| 单次输出 | 流式 65,536 / 非流式 32,768；超出后 `finishReason=CONTINUATION` + `continuationToken` | provider 走 SSE 流式，并自动续接（long decode），对上层是一次调用 |
| 续接规则 | 原样重发原始请求 + 顶层 `continuationToken`；`maxOutputTokens` 跨跳累计、只能单调增大；单请求 ≤20 分钟 | 不把已生成文本拼回 contents；请求超时设为 20 分钟 |
| 思考 | `thinkingLevel` = low / medium（默认）/ high；`thinkingBudget`、`minimal` 返回 400 | tier 映射为思考强度；模型 ID 支持 `@low/@medium/@high` 后缀 |
| 思考计费 | 思考 token 计入 `maxOutputTokens`，按输出价计费 | 在调用方的正文预算之上自动追加思考余量（low 4K / medium 16K / high 32K）；思考吃光预算且无正文时自动加倍续接一次 |
| 采样参数 | `temperature` / `top_p` / `top_k` 被忽略；`frequency_penalty` 等返回 400 | 不下发 temperature；多候选稿的差异来自模型自身采样 |
| 缓存 | 不支持显式缓存；≥6,144 token 共享前缀自动隐式缓存（95% 折扣），续接跳在 TTL 内同样命中 | system + 稳定上下文本就排在 prompt 最前，无需额外改造；命中量记入 `cache_read` 并按缓存价计费 |
| 对话 | 不支持以 model 回合结尾的预填充 | provider 直接拒绝，避免隐性 400 |
| 价格 | 2026-11-30 前 $2 / $10（缓存 $0.10）；之后 $4 / $20（缓存 $0.20），每百万 token | 预算熔断按标准价保守估算 |
| SDK | `google-genai >= 2.30.1` 才能自动续接；OpenAI 兼容层取不到长输出 | 不用 SDK / 兼容层，标准库 REST 直连，零新增依赖 |

资料来源：Google Cloud《Developer's guide to Gemini 4 Argon》《Get started with Gemini 4》、
Gemini API 参考《Generating content》（`continuationToken` / `CONTINUATION` / usage 字段）与《Long decode》。

## 2. 改造点

| 改造 | 位置 |
|---|---|
| 新增 `GeminiProvider`：SSE 流式、long decode 续接、思考强度、思考余量、隐式缓存计量、安全阈值、函数调用映射、错误码映射（429/5xx 可重试） | `novelforge/control_plane/llm/gemini_provider.py` |
| `CapabilitySet.long_output`：声明"单次可稳定超长输出" | `control_plane/llm/provider.py` |
| 工厂 `gemini` 分支 + `GEMINI_MODELS`（fast=`@low` / mid=`@medium` / strong=`@high`） | `control_plane/llm/factory.py` |
| 统一 env 解析 `resolve_provider_env` / `apply_provider_env`；Gemini key 优先推断 | `control_plane/llm/runtime.py` |
| 旧入口不再硬编码 `DEEPSEEK_API_KEY or ANTHROPIC_API_KEY`，统一走 runtime（修复只配 Gemini key 时退化为 fake 的问题） | `app/api/{projects,autopilot,cold_start,memory}.py`、`app/autopilot_manager.py`、`coldstart/batch.py` |
| 定价：`gemini-4-argon` + 隐式缓存价；自动剥离 `@思考强度` 后缀 | `control_plane/budget.py` |
| 可重试状态码补充 502 / 504 | `control_plane/llm/gateway.py` |
| **卷规划窗口按能力放宽**：普通模型 ≤10 章（防截断），长输出模型 ≤30 章；输出预算按每章约 1K token 线性放大 | `runtime.max_planning_horizon`、`planning/service.py`、`app/api/volumes.py`、`skills/volume_plan_skill.py` |

### 2.1 为什么三档都用 Gemini 4

`generate_validated` 的失败升级（fast→mid→strong）在 DeepSeek 下 mid 与 strong 是同一模型、升级意义有限；
Gemini 4 下升级 = 同模型加深推理（low→medium→high），解析失败时更可能被救回。
摘要、去重这类小任务若想省钱，可用 `NOVELFORGE_FAST_MODEL=gemini-3.8-flash` 等换成更便宜的模型。

### 2.2 正文生成

`chapter_draft` 原有的能力协商不变：中文正文预算 `min(max_tokens_out // 2, 32_000)`，Gemini 4 下取 32,000，
再加 strong 档 32K 思考余量 ≈ 64.8K，落在单次流式上限内，一般无需续接；更长的输出会自动续接。

## 3. 配置

```ini
NOVELFORGE_PROVIDER=gemini
GEMINI_API_KEY=xxx

# 可选
NOVELFORGE_STRONG_MODEL=gemini-4-argon@medium      # 降低正文/卷规划的思考强度以省钱提速
NOVELFORGE_FAST_MODEL=gemini-3.8-flash             # 小任务换便宜模型
NOVELFORGE_GEMINI_SAFETY_THRESHOLD=BLOCK_ONLY_HIGH # 题材含打斗/黑暗元素时减少误拦
GEMINI_BASE_URL=https://your-proxy.example         # 自建网关
```

被安全策略拦截时，provider 返回空正文与 `stop_reason="refusal"`，由 `generate_validated` 走同档重采样或升档。

## 4. 已知限制

- 未接入 Gemini embedding；向量召回仍用离线 hash 或 OpenAI 兼容 `/v1/embeddings`。
- 续接中途失败时由 gateway 整体重试（不做跳级断点续传）。
- 预算账本的 token 上限（默认 20 万/章）按"计费 token"累计；若开启 3 个候选稿且使用 high 思考，
  可酌情调高 `budget_max_tokens_per_chapter`。
