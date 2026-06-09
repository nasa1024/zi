"""NovelForge 运行时配置（全量可序列化 dataclass，无 ORM 依赖）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CanonGovernanceConfig:
    mode: str = "human_gate"  # "human_gate" | "auto_promote" | "hybrid"
    require_human_for: list = field(
        default_factory=lambda: ["knowledge_edge_change", "power_system", "retcon", "high"]
    )
    auto_promote_max_risk: str = "low"   # auto_promote 模式下允许自动的最高风险档
    evidence_threshold: float = 0.7      # evidence_strength 阈值（auto_promote 分支）


@dataclass
class BudgetConfig:
    max_tokens_per_chapter: int = 200_000
    max_usd_per_chapter: float = 2.0
    max_tokens_per_run: int = 1_000_000
    max_usd_per_run: float = 10.0


@dataclass
class ProviderConfig:
    provider: str = "anthropic"  # "anthropic" | "openai_compat" | "fake"
    api_key: Optional[str] = None
    base_url: Optional[str] = None        # openai_compat 专用
    fast_model: str = "claude-haiku-4-5-20251001"
    mid_model: str = "claude-sonnet-4-6"
    strong_model: str = "claude-opus-4-8"
    max_retries: int = 3


@dataclass
class RecallConfig:
    max_entities: int = 20
    max_keywords: int = 30
    context_window_chapters: int = 5     # 往前看多少章关键词
    enable_vector: bool = False          # MVP1 关闭向量召回


@dataclass
class DeduplicationConfig:
    bm25_gap_min: float = 2.0            # FTS BM25 分数差最小值（越大越宽松）
    enable_llm_arbiter: bool = True      # 是否启用 LLM 仲裁模糊 case


@dataclass
class NovelForgeConfig:
    db_path: str = "novel.db"
    project_id: str = "default"
    governance: CanonGovernanceConfig = field(default_factory=CanonGovernanceConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    recall: RecallConfig = field(default_factory=RecallConfig)
    dedup: DeduplicationConfig = field(default_factory=DeduplicationConfig)
    max_revise_loops: int = 2            # REVISE 阶段最大迭代次数
    draft_target_chars: int = 3000       # 目标字数（约 3000 汉字/章）
