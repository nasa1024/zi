"""NovelForge 运行时配置（全量可序列化 dataclass，无 ORM 依赖）。"""
from __future__ import annotations

import os
import json
import sqlite3
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
    enable_summaries: bool = True        # M2-②: 分层叙事摘要（章摘要+卷滚动摘要）
    summary_window_chapters: int = 5     # 注入最近 N 章摘要


@dataclass
class MemoryConfig:
    """§06 异步记忆管线 L2/L3 + 向量召回。默认全关 → 零额外 LLM/计算，旧行为不变。

    - L2 场景聚合：把每章正文切成场景块、各生成短摘要写 l2_scenes（向量召回的语料）。
    - L3 事件驱动：确定性扫 canon 级信号（世界规则/新实体/伏笔/约束）→ LLM 归一化
      为 BibleChangeProposal 进 staging（proposed，走人审兜底，绝不直接 commit）。
    - 向量召回：embed l2_scenes 场景摘要，召回时与 BM25 做 RRF 融合（§06.4.3）。
      实际开关复用 RecallConfig.enable_vector；本类只放调参。
    """
    enable_l2: bool = False              # L2 场景聚合（写 l2_scenes）
    l2_every_n_chapters: int = 4         # L2 触发间隔（§06.2.2 每 3-5 章；卷边界也触发）
    l2_max_scenes_per_chapter: int = 6   # 单章最多切几个场景块
    l2_tier: str = "fast"                # 场景摘要 LLM 档位
    enable_l3: bool = False              # L3 事件驱动 canon 提案
    l3_tier: str = "mid"                 # canon 归一化 LLM 档位
    vector_top_k: int = 8                # 向量召回返回的相似场景数
    rrf_k: int = 60                      # §06.4.3 RRF 融合常数 k=60
    embedding_dim: int = 256             # HashEmbedder 向量维度（hash provider 专用）
    # embedding 后端可插拔：默认离线 hash；可切第三方 /v1/embeddings（OpenAI/DeepSeek/vLLM…）
    embedding_provider: str = "hash"     # "hash" | "openai"
    embedding_model: str = "text-embedding-3-small"   # openai provider 的模型 id（维度由模型决定）
    embedding_base_url: Optional[str] = None          # openai 兼容端点（None=官方）
    embedding_api_key: Optional[str] = None           # 不填则回退环境变量


@dataclass
class DeduplicationConfig:
    bm25_gap_min: float = 2.0            # FTS BM25 分数差最小值（越大越宽松）
    enable_llm_arbiter: bool = True      # 是否启用 LLM 仲裁模糊 case


@dataclass
class CandidateConfig:
    """M3-①: 章节多候选 + 评分择优。n_candidates=1 时完全等价于旧行为。"""
    n_candidates: int = 1                # 候选稿数量（1=关闭；建议生产 2-3，上限 3）
    judge_tier: str = "mid"              # LLM 评委档位 fast|mid|strong
    temperature_spread: float = 0.15     # 候选间温度差（多样性）


@dataclass
class QualityConfig:
    """M5-⑦: 章节质量分门控 + 软问题润色。enabled=False 时零额外 LLM 调用。"""
    enabled: bool = False
    min_score: float = 6.0               # 低于此分触发润色 / 计入 autopilot 降级（autonovel 同款阈值）
    polish_enabled: bool = True          # 低分或 craft warn≥3 时做一轮润色（取分高版本，防润色变差）


@dataclass
class SettleConfig:
    """P1#6: 章末伏笔结算（mention/advance/payoff 判定 + 新伏笔确定性仲裁）。"""
    enabled: bool = True                 # 默认开；FAST 档单章成本可忽略
    tier: str = "fast"                   # 结算 LLM 档位
    max_new_hooks: int = 2               # 每章自动新建伏笔上限（防账本灌水）


@dataclass
class NovelForgeConfig:
    db_path: str = "novel.db"
    project_id: str = "default"
    governance: CanonGovernanceConfig = field(default_factory=CanonGovernanceConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    recall: RecallConfig = field(default_factory=RecallConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    dedup: DeduplicationConfig = field(default_factory=DeduplicationConfig)
    candidates: CandidateConfig = field(default_factory=CandidateConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    settle: SettleConfig = field(default_factory=SettleConfig)
    max_revise_loops: int = 2            # REVISE 阶段最大迭代次数
    patch_revise: bool = True            # M7: revise/润色先尝试锚点补丁（局部修改），失败回退全文重写
    midpoint_boost: bool = True          # M2-⑤: 卷中段(40-60%)章节 revise 上限 +1（ConStory 实证错误高发区）
    draft_target_chars: int = 3000       # 目标字数（约 3000 汉字/章）


# ── 运行期环境变量覆盖（集中入口；pipeline / autopilot 共用）──────────────────
#
# 运行期配置 = dataclass 安全默认 + 项目 memory_settings + 环境变量 + 每请求 API 字段。
# 下列 helper 把"记忆管线 / 向量召回 / 第三方 embedding"相关 env 注入 cfg，
# 默认全部不设 → 行为不变。LLM provider/key 的 env 仍在各 API 站点单独处理。

def _env_bool(name: str) -> Optional[bool]:
    v = os.environ.get(name)
    if v is None:
        return None
    normalized = v.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    return None


def _env_int(name: str) -> Optional[int]:
    v = os.environ.get(name)
    if v is None:
        return None
    try:
        return int(v.strip())
    except (ValueError, AttributeError):
        return None


def _env_bounded_int(name: str, minimum: int, maximum: int) -> Optional[int]:
    value = _env_int(name)
    if value is None or not minimum <= value <= maximum:
        return None
    return value


def apply_embedding_env(cfg: "NovelForgeConfig") -> None:
    """第三方 embedding 后端 env → cfg.memory（不设 PROVIDER 则保持默认离线 hash）。"""
    provider = os.environ.get("NOVELFORGE_EMBEDDING_PROVIDER")
    if not provider:
        return
    provider = provider.strip().lower()
    if provider not in {"hash", "openai"}:
        return
    cfg.memory.embedding_provider = provider
    model = os.environ.get("NOVELFORGE_EMBEDDING_MODEL")
    if model:
        cfg.memory.embedding_model = model
    cfg.memory.embedding_base_url = (
        os.environ.get("NOVELFORGE_EMBEDDING_BASE_URL") or cfg.provider.base_url
    )
    cfg.memory.embedding_api_key = (
        os.environ.get("NOVELFORGE_EMBEDDING_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
    )


def apply_memory_env(cfg: "NovelForgeConfig") -> None:
    """L2/L3/向量召回开关 + 调参 env → cfg（不设则保持当前项目设置）。"""
    b = _env_bool("NOVELFORGE_ENABLE_L2")
    if b is not None:
        cfg.memory.enable_l2 = b
    b = _env_bool("NOVELFORGE_ENABLE_L3")
    if b is not None:
        cfg.memory.enable_l3 = b
    b = _env_bool("NOVELFORGE_ENABLE_VECTOR")
    if b is not None:
        cfg.recall.enable_vector = b
    i = _env_bounded_int("NOVELFORGE_L2_EVERY_N", 1, 50)
    if i is not None:
        cfg.memory.l2_every_n_chapters = i
    i = _env_bounded_int("NOVELFORGE_L2_MAX_SCENES", 1, 20)
    if i is not None:
        cfg.memory.l2_max_scenes_per_chapter = i
    i = _env_bounded_int("NOVELFORGE_VECTOR_TOP_K", 1, 50)
    if i is not None:
        cfg.memory.vector_top_k = i
    i = _env_bounded_int("NOVELFORGE_RRF_K", 1, 1000)
    if i is not None:
        cfg.memory.rrf_k = i
    i = _env_bounded_int("NOVELFORGE_EMBEDDING_DIM", 1, 8192)
    if i is not None:
        cfg.memory.embedding_dim = i


def apply_runtime_env(cfg: "NovelForgeConfig") -> None:
    """集中应用记忆/向量/embedding 的 env 覆盖。pipeline 与 autopilot 路径共用。"""
    apply_embedding_env(cfg)
    apply_memory_env(cfg)


# ── 项目级记忆设置 ────────────────────────────────────────────────────────────

PROJECT_MEMORY_META_KEY = "memory_settings"
DEFAULT_PROJECT_MEMORY_SETTINGS: dict[str, object] = {
    # 产品默认开启 L2 + 离线 hash 向量；低层 dataclass 仍保持关闭，方便测试/脚本显式控制。
    "enable_l2": True,
    "enable_vector": True,
    "enable_l3": False,
    "enable_summaries": True,
    "l2_every_n_chapters": 4,
    "l2_max_scenes_per_chapter": 6,
    "vector_top_k": 8,
    "summary_window_chapters": 5,
}

_PROJECT_MEMORY_BOOL_FIELDS = {
    "enable_l2", "enable_vector", "enable_l3", "enable_summaries",
}
_PROJECT_MEMORY_INT_RANGES: dict[str, tuple[int, int]] = {
    "l2_every_n_chapters": (1, 50),
    "l2_max_scenes_per_chapter": (1, 20),
    "vector_top_k": (1, 50),
    "summary_window_chapters": (1, 50),
}


def _validated_project_memory_updates(raw: object) -> dict[str, object]:
    """只接受类型正确且位于产品范围内的项目设置。

    meta_kv 可能来自旧版本或被手工编辑；这里不能用 ``bool('false')`` 或
    ``int(...)`` 强转，否则损坏数据会静默改变运行时行为。
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, object] = {}
    for key in _PROJECT_MEMORY_BOOL_FIELDS:
        value = raw.get(key)
        if isinstance(value, bool):
            out[key] = value
    for key, (minimum, maximum) in _PROJECT_MEMORY_INT_RANGES.items():
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            if minimum <= value <= maximum:
                out[key] = value
    return out


def load_project_memory_settings(conn: sqlite3.Connection) -> dict[str, object]:
    """读取项目设置；未保存过时返回面向产品的默认值。"""
    settings = dict(DEFAULT_PROJECT_MEMORY_SETTINGS)
    try:
        row = conn.execute(
            "SELECT value FROM meta_kv WHERE key=?", (PROJECT_MEMORY_META_KEY,)
        ).fetchone()
        if row and row["value"]:
            raw = json.loads(row["value"])
            settings.update(_validated_project_memory_updates(raw))
    except (sqlite3.Error, TypeError, json.JSONDecodeError):
        pass
    return settings


def save_project_memory_settings(
    conn: sqlite3.Connection, updates: dict[str, object]
) -> dict[str, object]:
    """合并并保存项目记忆设置，返回完整设置。"""
    settings = load_project_memory_settings(conn)
    settings.update(_validated_project_memory_updates(updates))
    conn.execute(
        "INSERT INTO meta_kv(key,value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
        (PROJECT_MEMORY_META_KEY, json.dumps(settings, ensure_ascii=False, sort_keys=True)),
    )
    conn.commit()
    return settings


def apply_project_memory_settings(
    cfg: "NovelForgeConfig", conn: sqlite3.Connection
) -> dict[str, object]:
    """把项目设置注入 cfg；调用后再 apply_runtime_env，使运维 env 拥有最高优先级。"""
    settings = load_project_memory_settings(conn)
    cfg.memory.enable_l2 = settings["enable_l2"]
    cfg.memory.enable_l3 = settings["enable_l3"]
    cfg.recall.enable_vector = settings["enable_vector"]
    cfg.recall.enable_summaries = settings["enable_summaries"]
    cfg.memory.l2_every_n_chapters = settings["l2_every_n_chapters"]
    cfg.memory.l2_max_scenes_per_chapter = settings["l2_max_scenes_per_chapter"]
    cfg.memory.vector_top_k = settings["vector_top_k"]
    cfg.recall.summary_window_chapters = settings["summary_window_chapters"]
    return settings


def effective_memory_settings(cfg: "NovelForgeConfig") -> dict[str, object]:
    return {
        "enable_l2": cfg.memory.enable_l2,
        "enable_vector": cfg.recall.enable_vector,
        "enable_l3": cfg.memory.enable_l3,
        "enable_summaries": cfg.recall.enable_summaries,
        "l2_every_n_chapters": cfg.memory.l2_every_n_chapters,
        "l2_max_scenes_per_chapter": cfg.memory.l2_max_scenes_per_chapter,
        "vector_top_k": cfg.memory.vector_top_k,
        "summary_window_chapters": cfg.recall.summary_window_chapters,
        "embedding_provider": cfg.memory.embedding_provider,
        "embedding_model": cfg.memory.embedding_model,
    }


def memory_env_overrides() -> list[str]:
    overrides: list[str] = []
    for env, field in (
        ("NOVELFORGE_ENABLE_L2", "enable_l2"),
        ("NOVELFORGE_ENABLE_L3", "enable_l3"),
        ("NOVELFORGE_ENABLE_VECTOR", "enable_vector"),
    ):
        if _env_bool(env) is not None:
            overrides.append(field)
    for env, field, minimum, maximum in (
        ("NOVELFORGE_L2_EVERY_N", "l2_every_n_chapters", 1, 50),
        ("NOVELFORGE_L2_MAX_SCENES", "l2_max_scenes_per_chapter", 1, 20),
        ("NOVELFORGE_VECTOR_TOP_K", "vector_top_k", 1, 50),
    ):
        if _env_bounded_int(env, minimum, maximum) is not None:
            overrides.append(field)
    provider = (os.environ.get("NOVELFORGE_EMBEDDING_PROVIDER") or "").strip().lower()
    if provider in {"hash", "openai"}:
        overrides.append("embedding_provider")
        if os.environ.get("NOVELFORGE_EMBEDDING_MODEL"):
            overrides.append("embedding_model")
    return overrides
