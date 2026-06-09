# NovelForge

面向**网文 / 长篇连载**的 AI 写作 **记忆 + 一致性 + 工艺** 引擎。Python 3.13 + FastAPI、本地优先、单一 SQLite（`novel.db`）、单人可开发可维护。

> 起点是 [`require.md`](require.md)（借鉴 TencentDB-Agent-Memory 分层记忆的草案）。经多轮多智能体评审后**重新设计**，纠正了三处地基级问题：
> 1. **canon 是只追加的结构化账本**（`facts`/`fact_revisions` + 只读渲染视图），不是 LLM 重写的 markdown；
> 2. **硬一致性靠 World State Store + 确定性 Python validator + as-of 投影**，不是纯检索 + LLM；
> 3. **双模式（人审 / 全自动）是同一条管线在唯一晋升闸门处分叉**，靠一行 config 切换。
>
> 并新增与一致性正交的**网文工艺层**（爽点 / 钩子 / 节奏 / value_shift）——一致性是不扣分项、追更力是得分项。

## 设计哲学（5 条）

- 软记忆走 RAG，**硬状态走关系表 + 确定性 validator**。
- canon **只追加**，LLM 只产 `BibleChangeProposal`，永不直写 canon。
- 一致性 = **写时约束（as-of 注入）+ 事后兜底**。
- 检索**以实体 / 关系为主**、语义为辅。
- 成本 / 缓存是连写形态的生死线（稳定前缀走 prompt cache、断路器）。

## 快速开始

```bash
pip install fastapi uvicorn jieba pydantic

# 启动 API 服务（开发模式，无需 API key）
uvicorn novelforge.app.main:app --host 127.0.0.1 --port 8787 --reload

# 新建项目
curl -s -X POST http://localhost:8787/v1/projects \
  -H "Content-Type: application/json" \
  -d '{"name":"我的小说","genre":"xuanhuan"}' | python -m json.tool

# 全量测试（265 个用例）
python -m pytest -q
```

## 前端（`web/`，Vite + React + TypeScript）

营销官网（霓虹蛮荒多巴胺风格落地页）+ 创作工作台 Studio（真实接入后端 REST API）。

```bash
# 1) 先起后端（同上，:8787）
uvicorn novelforge.app.main:app --port 8787

# 2) 再起前端（:5173，经 Vite proxy 调 API，无跨域）
cd web && npm install && npm run dev
```

打开 http://localhost:5173 ——落地页下方的 **⚙️ 工作台 / STUDIO** 真实驱动确定性核心（无需 LLM）：

- **建项目** → `POST /v1/projects`（乐观更新，新书即时可用）
- **录入设定 / 填充示例** → `POST /v1/{id}/seed`（三元组写入 canon 账本）
- **世界圣经** → `GET /v1/{id}/bible`（由 canon 确定性渲染 Markdown）
- **世界状态** → `POST /v1/{id}/state`（as-of 时点投影角色境界）
- **设定检索** → `GET /v1/{id}/search/facts`（FTS5 + BM25）
- **审核队列** → `GET /v1/{id}/reviews` + `/staging`，逐条 approve/reject
- **生成章节** → `POST /v1/{id}/pipeline/run`（需配置 LLM provider key）

实时引擎健康徽章读 `GET /health`。详见 [`web/README.md`](web/README.md)。
后端已配 CORS（允许 `:5173/:4173`）；生产可经 `NOVELFORGE_CORS_ORIGINS` 覆盖、`VITE_API_BASE` 指定 API 地址。

## 配置 LLM Provider Key（生成章节用）

确定性核心（seed / bible / state / search / 审核）**无需 LLM**。只有「生成章节」
（`pipeline/run`）需要 LLM provider key，否则后端默认回退到 `anthropic` 且无 key，
会报 *"Could not resolve authentication method"*。

在项目根建 `.env`（已被 `.gitignore` 忽略，密钥不入库）：

```ini
# DeepSeek（推荐，OpenAI 兼容）
DEEPSEEK_API_KEY=sk-你的key
NOVELFORGE_PROVIDER=deepseek
```

然后用启动脚本（自动读 `.env`）拉起后端：

```powershell
.\run-backend.ps1          # Windows PowerShell
```

```bash
set -a; . ./.env; set +a; uvicorn novelforge.app.main:app --port 8787   # bash
```

> ⚠️ **不要**把 LLM key 放进 `NOVELFORGE_API_KEY`——那个变量是 **REST 鉴权** 用的，
> 一旦设置，所有 `/v1/*` 请求都会要求 Bearer token，前端会收到 401。
> LLM key 用 `DEEPSEEK_API_KEY`（或 `ANTHROPIC_API_KEY` + `NOVELFORGE_PROVIDER=anthropic`）。

## 代码结构（`novelforge/`，MVP 已全量实现）

| 模块 | 说明 |
|---|---|
| `db/schema.sql` | §02 全量 DDL（SCHEMA_VERSION=7）：canon / World State / 治理 / 工艺 / FTS5 / 分支 / pipeline_run / sessions |
| `db/connection.py` | 建库 / PRAGMA / `rebuild_facts_fts` / 启动期孤儿扫描 |
| `db/migrations/` | v3→v7 增量迁移器（volumes/branches/pipeline_run/sessions/turns） |
| `db/l0.py` | L0 原子写入（temp→fsync→rename）+ orphan sweep + crashed_runs sweep |
| `db/write.py` | 写入辅助函数 |
| `world/replay.py` | as-of 投影（replay_power/knowledge/items/numeric/gimmick/timeline），支持 branch_id |
| `world/branch.py` | 分支祖先链 SQL 过滤器（§9.4 分支隔离） |
| `world/projection.py` | World State Store 投影 |
| `validators/` | 确定性 Python 验证器（power / knowledge / timeline / numeric / gimmick） |
| `governance/` | Promote / Revert / Gate / Conflict 治理层 |
| `control_plane/orchestrator.py` | 主循环：plan→recall→draft→check→gate，pipeline_run 状态机 |
| `control_plane/llm/` | 多供应商 LLM 接入层（DeepSeek / OpenAI-compat / Anthropic / Fake） |
| `control_plane/budget.py` | Token/USD 预算 + 断路器 |
| `skills/` | 5 个核心 Skill：planner / chapter_draft / continuity_check / craft_check / cold_extract |
| `dedup/` | DeduplicationEngine（BM25 + 可选 LLM 裁判） |
| `memory/recall.py` | 实体优先召回 + FTS5 关键词检索 |
| `memory/bible_render.py` | World Bible Markdown 渲染 |
| `craft/` | Pacing Controller + PayoffBinding + VoiceProfile |
| `app/main.py` | FastAPI 应用装配（API-key 认证中间件） |
| `app/api/` | REST 路由：projects / memory / governance / orchestrator / autopilot / volumes / sessions / admin |
| `app/security.py` | Bearer/X-API-Key 认证 + Prompt Injection 净化（11 条正则） |
| `ids.py` | 前缀化 ID 生成 |
| `tokenizer.py` | FTS5 应用层预分词（jieba 可选，缺失回退 bigram） |

## API 一览（`/v1` 前缀）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/projects` | 新建项目 |
| GET | `/projects` | 列出所有项目 |
| POST | `/{pid}/seed` | 批量播种 canon facts（冷启动） |
| POST | `/{pid}/pipeline/run` | 生成一章（完整 plan→draft→check→gate 流水线） |
| GET | `/{pid}/facts` | 查询 facts（支持 FTS 关键词） |
| POST | `/{pid}/governance/review` | 人工审批 review queue 条目 |
| POST | `/{pid}/governance/revert` | 回滚 fact 到指定版本 |
| POST/GET | `/{pid}/volumes` | 卷管理 |
| POST/GET | `/{pid}/branches` | 分支管理 |
| POST | `/{pid}/sessions` | 创建会话（CLI/Web/Chat/API） |
| POST | `/{pid}/sessions/{sid}/turns` | 创建 turn（同步或 SSE 流式） |
| GET | `/{pid}/sessions/{sid}/turns/{tid}/stream` | SSE 断线续传 |
| POST | `/{pid}/admin/backup` | 热备份（novel.db + l0/） |
| POST | `/{pid}/admin/rebuild_fts` | 全量重建 FTS5 索引 |
| POST | `/{pid}/admin/add_terms` | 增量追加 jieba 词典 |
| GET | `/health` | 健康检查（无需认证） |

## 关键设计决策

### 确定性一致性
- `character_power_log` / `knowledge_edges` / `item_log` / `numeric_facts` / `gimmick_rules` 均以 `source_fact_id` 锚定 canon 来源
- as-of 投影 = LEFT JOIN facts ON source_fact_id，筛 `status='canon' AND valid_from_chapter<=N`
- 硬冲突由 Python validator 拦截，软冲突走 LLM 二次判断

### L0 原子写入（F7）
- 草稿落盘：`tmp = write + fsync → os.replace(final)`，同目录保证同文件系统
- `pipeline_run` 状态机：启动时写 `'running'`，完成时更新 `'completed'`；下次启动扫描残留 `'running'` → `'crashed'`

### 分支隔离（§9.4）
- `facts.branch_id = NULL` 为主线，非 NULL 为分支专属
- `replay_*(conn, N, branch_id=X)` 自动遍历祖先链，按 fork_chapter 截断每级可见范围
- 向后兼容：`branch_id=None` 不施加额外过滤

### 双模式治理
- `auto_promote`：低风险 fact 直接晋升 canon
- `human_gate`：所有 fact 入 review queue 等人工批准
- `hybrid`：低/中风险自动，高风险入队

## 文档

设计文档在 [`docs/NovelForge/`](docs/NovelForge/)（共 15 节，从 `00-总览与阅读指南.md` 入）：

| 节 | 主题 |
|---|---|
| 01–02 | 总体架构 · 存储与数据模型（完整 SQLite DDL） |
| 03–04 | Canon 治理与双模式 · 一致性引擎（确定性 validators） |
| 05–06 | 网文工艺层 · 记忆管线与召回 |
| 07–08 | Skill 体系与主循环 · API/配置/工程落地 |
| 09 | MVP 路线图与验收 |
| 10–11 | 数据契约与命名权威 · 核心算法补全 |
| 12–14 | Agent 循环与工具调用 · 交互模型（CLI/REST/Web/对话式）· LLM 多供应商接入层 |

实现级规格（可直接照着写代码）在 [`docs/NovelForge/impl/`](docs/NovelForge/impl/)。

## 测试

```
265 passed in ~15s
```

覆盖范围：schema 迁移、L0 原子写入、pipeline_run 状态机、会话/turn/SSE、API-key 认证、Prompt Injection 净化、热备份、FTS 重建、分支隔离 World State、软冲突检查、卷/分支 CRUD、冷启动、工艺层、自动驾驶、端到端 MVP 流水线。
