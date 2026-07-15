"""Project-scoped LLM runtime assembly must never cross-wire credentials."""
from __future__ import annotations

import pytest

from novelforge.control_plane.llm.runtime import build_project_config


_ENV_KEYS = (
    "NOVELFORGE_PROVIDER",
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "NOVELFORGE_OPENAI_BASE_URL",
)


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_KEYS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("requested", "key_name", "expected_provider"),
    [
        ("deepseek", "DEEPSEEK_API_KEY", "deepseek"),
        ("deepseek_anthropic", "DEEPSEEK_API_KEY", "deepseek_anthropic"),
        ("anthropic", "ANTHROPIC_API_KEY", "anthropic"),
        ("openai", "OPENAI_API_KEY", "openai_compat"),
        ("openai_compat", "OPENAI_API_KEY", "openai_compat"),
    ],
)
def test_explicit_provider_uses_only_its_matching_key(
    monkeypatch: pytest.MonkeyPatch,
    requested: str,
    key_name: str,
    expected_provider: str,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("NOVELFORGE_PROVIDER", requested)
    monkeypatch.setenv(key_name, "matching-key")
    # A higher-precedence unrelated key must not be paired with the request.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "matching-key" if key_name == "DEEPSEEK_API_KEY" else "wrong-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "matching-key" if key_name == "ANTHROPIC_API_KEY" else "wrong-key")
    monkeypatch.setenv("OPENAI_API_KEY", "matching-key" if key_name == "OPENAI_API_KEY" else "wrong-key")

    cfg = build_project_config("project", "novel.db")

    assert cfg.provider.provider == expected_provider
    assert cfg.provider.api_key == "matching-key"


def test_explicit_provider_does_not_borrow_an_unrelated_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("NOVELFORGE_PROVIDER", "anthropic")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-only")

    cfg = build_project_config("project", "novel.db")

    assert cfg.provider.provider == "anthropic"
    assert cfg.provider.api_key is None


def test_provider_is_inferred_from_the_available_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-only")

    cfg = build_project_config("project", "novel.db")

    assert cfg.provider.provider == "openai_compat"
    assert cfg.provider.api_key == "openai-only"
