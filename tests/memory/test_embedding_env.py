"""_make_cfg 的 embedding 环境变量接线测试。

不设 env → 默认 hash（旧行为）；设 NOVELFORGE_EMBEDDING_PROVIDER → 进 cfg.memory。
"""
from __future__ import annotations

import pytest

from novelforge.app.api.orchestrator import _apply_embedding_env
from novelforge.config import NovelForgeConfig


def test_no_env_keeps_hash(monkeypatch):
    for k in ("NOVELFORGE_EMBEDDING_PROVIDER", "NOVELFORGE_EMBEDDING_MODEL",
              "NOVELFORGE_EMBEDDING_BASE_URL", "NOVELFORGE_EMBEDDING_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = NovelForgeConfig()
    _apply_embedding_env(cfg)
    assert cfg.memory.embedding_provider == "hash"


def test_env_switches_to_openai(monkeypatch):
    monkeypatch.setenv("NOVELFORGE_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("NOVELFORGE_EMBEDDING_MODEL", "text-embedding-3-large")
    monkeypatch.setenv("NOVELFORGE_EMBEDDING_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("NOVELFORGE_EMBEDDING_API_KEY", "sk-test")
    cfg = NovelForgeConfig()
    _apply_embedding_env(cfg)
    assert cfg.memory.embedding_provider == "openai"
    assert cfg.memory.embedding_model == "text-embedding-3-large"
    assert cfg.memory.embedding_base_url == "https://api.example.com/v1"
    assert cfg.memory.embedding_api_key == "sk-test"


def test_env_falls_back_to_llm_creds(monkeypatch):
    """未单独给 embedding base_url/key 时回退到 LLM 的凭据（同端点常可复用）。"""
    for k in ("NOVELFORGE_EMBEDDING_MODEL", "NOVELFORGE_EMBEDDING_BASE_URL",
              "NOVELFORGE_EMBEDDING_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NOVELFORGE_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("NOVELFORGE_API_KEY", "sk-llm")
    cfg = NovelForgeConfig()
    cfg.provider.base_url = "https://llm.example.com/v1"
    _apply_embedding_env(cfg)
    assert cfg.memory.embedding_base_url == "https://llm.example.com/v1"
    assert cfg.memory.embedding_api_key == "sk-llm"


def test_build_embedder_from_env_cfg(monkeypatch):
    """端到端：env → cfg → build_embedder 得到 OpenAIEmbedder（不触发网络/导入）。"""
    from novelforge.memory.embedding import OpenAIEmbedder, build_embedder

    monkeypatch.setenv("NOVELFORGE_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("NOVELFORGE_EMBEDDING_MODEL", "bge-m3")
    cfg = NovelForgeConfig()
    _apply_embedding_env(cfg)
    emb = build_embedder(cfg.memory)
    assert isinstance(emb, OpenAIEmbedder)
    assert emb.version == "bge-m3"
