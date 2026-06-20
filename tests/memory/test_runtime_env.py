"""集中式运行期 env 覆盖测试：apply_memory_env / apply_runtime_env。

使 L2/L3/向量开关 + 调参可由环境变量控制（默认全关不变）。
"""
from __future__ import annotations

import pytest

from novelforge.config import (
    NovelForgeConfig,
    apply_memory_env,
    apply_runtime_env,
)

_MEM_KEYS = (
    "NOVELFORGE_ENABLE_L2", "NOVELFORGE_ENABLE_L3", "NOVELFORGE_ENABLE_VECTOR",
    "NOVELFORGE_L2_EVERY_N", "NOVELFORGE_L2_MAX_SCENES",
    "NOVELFORGE_VECTOR_TOP_K", "NOVELFORGE_RRF_K", "NOVELFORGE_EMBEDDING_DIM",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _MEM_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_no_env_keeps_defaults():
    cfg = NovelForgeConfig()
    apply_memory_env(cfg)
    assert cfg.memory.enable_l2 is False
    assert cfg.memory.enable_l3 is False
    assert cfg.recall.enable_vector is False


def test_bool_toggles(monkeypatch):
    monkeypatch.setenv("NOVELFORGE_ENABLE_L2", "true")
    monkeypatch.setenv("NOVELFORGE_ENABLE_L3", "1")
    monkeypatch.setenv("NOVELFORGE_ENABLE_VECTOR", "on")
    cfg = NovelForgeConfig()
    apply_memory_env(cfg)
    assert cfg.memory.enable_l2 is True
    assert cfg.memory.enable_l3 is True
    assert cfg.recall.enable_vector is True


def test_bool_false_values(monkeypatch):
    monkeypatch.setenv("NOVELFORGE_ENABLE_L2", "false")
    monkeypatch.setenv("NOVELFORGE_ENABLE_VECTOR", "0")
    cfg = NovelForgeConfig()
    cfg.memory.enable_l2 = True            # 起始为 True，env 应把它关掉
    cfg.recall.enable_vector = True
    apply_memory_env(cfg)
    assert cfg.memory.enable_l2 is False
    assert cfg.recall.enable_vector is False


def test_int_tuning(monkeypatch):
    monkeypatch.setenv("NOVELFORGE_L2_EVERY_N", "3")
    monkeypatch.setenv("NOVELFORGE_VECTOR_TOP_K", "12")
    monkeypatch.setenv("NOVELFORGE_EMBEDDING_DIM", "512")
    cfg = NovelForgeConfig()
    apply_memory_env(cfg)
    assert cfg.memory.l2_every_n_chapters == 3
    assert cfg.memory.vector_top_k == 12
    assert cfg.memory.embedding_dim == 512


def test_bad_int_ignored(monkeypatch):
    monkeypatch.setenv("NOVELFORGE_L2_EVERY_N", "not-a-number")
    cfg = NovelForgeConfig()
    apply_memory_env(cfg)
    assert cfg.memory.l2_every_n_chapters == 4   # 默认未被破坏


def test_apply_runtime_env_does_both(monkeypatch):
    monkeypatch.setenv("NOVELFORGE_ENABLE_VECTOR", "true")
    monkeypatch.setenv("NOVELFORGE_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("NOVELFORGE_EMBEDDING_MODEL", "bge-m3")
    cfg = NovelForgeConfig()
    apply_runtime_env(cfg)
    assert cfg.recall.enable_vector is True              # memory 侧
    assert cfg.memory.embedding_provider == "openai"     # embedding 侧
    assert cfg.memory.embedding_model == "bge-m3"
