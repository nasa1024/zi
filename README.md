# NovelForge

面向**网文 / 长篇连载**的 AI 写作 **记忆 + 一致性 + 工艺** 引擎。Python + FastAPI、本地优先、单一 SQLite（`novel.db`）、单人可开发可维护。

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
| 12–14 | **Agent 循环与工具调用 · 交互模型（CLI/REST/Web/对话式）· LLM 多供应商接入层** |

实现级规格（可直接照着写代码）在 [`docs/NovelForge/impl/`](docs/NovelForge/impl/)：§12 工具循环 + §14 LLM 接入层 + 二者接缝契约。

## 代码（`novelforge/`，MVP0 进行中）

| 模块 | 状态 |
|---|---|
| `db/schema.sql` | §02 全量 DDL（canon / World State / 治理 / 工艺 / 检索 FTS5） |
| `db/connection.py` | 建库 / PRAGMA / `rebuild_facts_fts` |
| `ids.py` | 前缀化 ID 生成 |
| `tokenizer.py` | FTS5 应用层预分词（jieba 可选，缺失回退 bigram） |

待补（MVP0）：`contracts.py`、World State 仓储、5 个确定性 validator、实体优先 Recall、CLI、pytest。LLM/向量为可选依赖，零外网即可建库与单测。

```bash
python -c "from novelforge import db; c=db.init_db('novel.db'); print('ok')"
```
