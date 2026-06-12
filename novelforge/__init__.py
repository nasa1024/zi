"""NovelForge — 面向网文连载的 AI 写作记忆 + 一致性 + 工艺引擎 (MVP0).

设计文档见 docs/NovelForge/。本包实现 MVP0：确定性内核（建库 + World State +
确定性 validators + FTS 实体召回 + 人工 canon + 管线骨架），LLM/jieba/向量均为可选。
"""

__version__ = "0.1.0"
SCHEMA_VERSION = "11"  # +逐章成本: pipeline_run.tokens_spent/usd_spent
