"""build_gateway：根据 NovelForgeConfig 构造 LLMGateway。

用法：
    from novelforge.control_plane.llm.factory import build_gateway
    gw = build_gateway(cfg, ledger)
"""
from __future__ import annotations

from typing import Optional

from ...config import NovelForgeConfig, ProviderConfig
from ..budget import BudgetLedger
from .gateway import LLMGateway
from .provider import LLMProvider
from .tiers import ModelTier

# DeepSeek 预置模型 ID
DEEPSEEK_MODELS = {
    "fast":   "deepseek-v4-flash",  # V4 Flash：速度快，低成本
    "mid":    "deepseek-v4-pro",    # V4 Pro：主力档，1M 上下文 / 384K 输出
    "strong": "deepseek-v4-pro",    # V4 Pro：同上，thinking 模式推理强
}

# DeepSeek base URL
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"

# Gemini 4 预置：三档同为 gemini-4-argon，以思考强度（thinkingLevel）区分档位——
# 失败升级 fast→mid→strong 即"同模型加深推理"。可用 NOVELFORGE_*_MODEL 覆盖，
# 例如 NOVELFORGE_FAST_MODEL=gemini-3.8-flash 让摘要类小任务走更便宜的模型。
GEMINI_MODELS = {
    "fast":   "gemini-4-argon@low",
    "mid":    "gemini-4-argon@medium",
    "strong": "gemini-4-argon@high",
}

# ProviderConfig 的默认（Claude）模型 ID：Gemini 下视为"未指定"
_DEFAULT_PROVIDER_CONFIG = ProviderConfig()


def build_provider(pc: ProviderConfig) -> LLMProvider:
    """根据 ProviderConfig 实例化对应 provider。"""
    p = pc.provider.lower()
    if p == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(api_key=pc.api_key, base_url=pc.base_url)

    elif p == "deepseek_anthropic":
        # 用 Anthropic SDK 调 DeepSeek Anthropic 兼容端点
        # 优势：prompt caching / 工具调用 / 完整 Anthropic 特性
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(
            api_key=pc.api_key,
            base_url=pc.base_url or DEEPSEEK_ANTHROPIC_BASE_URL,
        )

    elif p == "deepseek":
        # 用 OpenAI SDK 调 DeepSeek OpenAI 兼容端点
        from .openai_compat_provider import OpenAICompatProvider
        return OpenAICompatProvider(
            api_key=pc.api_key,
            base_url=pc.base_url or DEEPSEEK_BASE_URL,
        )

    elif p == "openai_compat":
        from .openai_compat_provider import OpenAICompatProvider
        return OpenAICompatProvider(api_key=pc.api_key, base_url=pc.base_url)

    elif p == "gemini":
        # 标准库 REST 实现（long decode 续接 / 思考强度），无需额外 SDK
        from .gemini_provider import GeminiProvider
        return GeminiProvider(
            api_key=pc.api_key,
            base_url=pc.base_url,
            safety_threshold=pc.safety_threshold,
        )

    elif p == "fake":
        from .fake_provider import FakeProvider
        return FakeProvider()

    else:
        raise ValueError(
            f"未知 provider: {pc.provider!r}。"
            f"支持: gemini / anthropic / deepseek_anthropic / deepseek / openai_compat / fake"
        )


def _model_map(pc: ProviderConfig) -> dict[str, str]:
    """将三档 tier 映射为具体 model ID。"""
    p = pc.provider.lower()
    if p == "gemini":
        base = dict(GEMINI_MODELS)
        for tier in ("fast", "mid", "strong"):
            chosen = getattr(pc, f"{tier}_model")
            if chosen and chosen != getattr(_DEFAULT_PROVIDER_CONFIG, f"{tier}_model"):
                base[tier] = chosen
        return base
    if p == "deepseek":
        base = dict(DEEPSEEK_MODELS)
    else:
        base = {
            "fast":   pc.fast_model,
            "mid":    pc.mid_model,
            "strong": pc.strong_model,
        }
    # 允许 ProviderConfig 字段覆盖
    if pc.fast_model and p != "deepseek":
        base["fast"] = pc.fast_model
    if pc.mid_model and p != "deepseek":
        base["mid"] = pc.mid_model
    if pc.strong_model and p != "deepseek":
        base["strong"] = pc.strong_model
    return base


def build_gateway(
    cfg: NovelForgeConfig,
    ledger: Optional[BudgetLedger] = None,
) -> LLMGateway:
    """一步构造 LLMGateway。ledger 为 None 时按 BudgetConfig 创建新的。"""
    if ledger is None:
        bc = cfg.budget
        ledger = BudgetLedger(
            max_tokens=bc.max_tokens_per_chapter,
            max_usd=bc.max_usd_per_chapter,
        )
    provider = build_provider(cfg.provider)
    return LLMGateway(
        provider,
        ledger,
        model_map=_model_map(cfg.provider),
        max_retries=cfg.provider.max_retries,
    )
