"""Gemini 4 provider：请求形态 / long decode 续接 / 思考强度 / 计费 / 运行时接线。

全部使用注入 transport 或打桩 urlopen，无网络。
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from novelforge.config import NovelForgeConfig, ProviderConfig
from novelforge.control_plane.budget import BudgetLedger, _estimate_cost
from novelforge.control_plane.llm import gemini_provider as gp
from novelforge.control_plane.llm.factory import GEMINI_MODELS, build_gateway
from novelforge.control_plane.llm.gateway import LLMGateway
from novelforge.control_plane.llm.gemini_provider import GeminiAPIError, GeminiProvider
from novelforge.control_plane.llm.provider import Message, Tool, Usage
from novelforge.control_plane.llm.runtime import (
    DEFAULT_PLANNING_HORIZON,
    LONG_OUTPUT_PLANNING_HORIZON,
    apply_provider_env,
    build_project_config,
    build_project_gateway,
    max_planning_horizon,
)
from novelforge.control_plane.llm.tiers import ModelTier


_ENV_KEYS = (
    "NOVELFORGE_PROVIDER",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_BASE_URL",
    "NOVELFORGE_GEMINI_BASE_URL",
    "NOVELFORGE_GEMINI_SAFETY_THRESHOLD",
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "NOVELFORGE_FAST_MODEL",
    "NOVELFORGE_MID_MODEL",
    "NOVELFORGE_STRONG_MODEL",
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in _ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _chunk(text=None, *, finish=None, token=None, usage=None, thought=None, call=None):
    parts = []
    if thought is not None:
        parts.append({"text": thought, "thought": True})
    if text is not None:
        parts.append({"text": text})
    if call is not None:
        parts.append({"functionCall": call})
    cand: dict = {"content": {"role": "model", "parts": parts}}
    if finish:
        cand["finishReason"] = finish
    if token:
        cand["continuationToken"] = token
    chunk: dict = {"candidates": [cand]}
    if usage:
        chunk["usageMetadata"] = usage
    return chunk


class _Transport:
    """按请求顺序返回预置分片列表，并记录每次请求。"""

    def __init__(self, *hops):
        self._hops = list(hops)
        self.calls: list[dict] = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({
            "url": url, "headers": headers, "body": json.loads(json.dumps(body)),
            "timeout": timeout,
        })
        hop = self._hops.pop(0)
        if isinstance(hop, Exception):
            raise hop
        return iter(hop)


def _provider(transport, **kw) -> GeminiProvider:
    return GeminiProvider(api_key="test-key", transport=transport, **kw)


# ── 请求形态 ───────────────────────────────────────────────────────────────────

def test_request_shape_uses_thinking_level_and_omits_sampling():
    t = _Transport([_chunk("正文", finish="STOP")])
    resp = _provider(t).generate(
        [
            Message("user", "第一段"),
            Message("assistant", "好的"),
            Message("user", "继续"),
            Message("user", "补充"),
        ],
        model="gemini-4-argon@high",
        max_tokens=8000,
        system="你是网文作者",
        temperature=0.7,
    )
    assert resp.text == "正文"
    assert resp.stop_reason == "end_turn"
    call = t.calls[0]
    assert call["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-4-argon:streamGenerateContent?alt=sse"
    )
    assert call["headers"]["x-goog-api-key"] == "test-key"
    body = call["body"]
    assert body["systemInstruction"] == {"parts": [{"text": "你是网文作者"}]}
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert body["contents"][2]["parts"] == [{"text": "继续"}, {"text": "补充"}]
    cfg = body["generationConfig"]
    # 可见正文预算 + high 思考余量；temperature 在 Gemini 4 上被忽略，不下发
    assert cfg["maxOutputTokens"] == 8000 + 32_768
    assert cfg["thinkingConfig"] == {"thinkingLevel": "high"}
    assert "temperature" not in cfg
    assert "safetySettings" not in body
    assert "continuationToken" not in body


def test_model_without_suffix_uses_server_default_thinking():
    t = _Transport([_chunk("ok", finish="STOP")])
    _provider(t).generate([Message("user", "hi")], model="gemini-4-argon", max_tokens=1000)
    cfg = t.calls[0]["body"]["generationConfig"]
    assert "thinkingConfig" not in cfg
    assert cfg["maxOutputTokens"] == 1000 + 16_384     # 按默认 medium 预留思考余量


def test_output_budget_capped_by_model_limit():
    t = _Transport([_chunk("ok", finish="STOP")])
    _provider(t).generate([Message("user", "hi")], model="gemini-3.8-flash@low", max_tokens=64_000)
    assert t.calls[0]["body"]["generationConfig"]["maxOutputTokens"] == 65_536


def test_invalid_thinking_suffix_rejected():
    with pytest.raises(ValueError, match="思考强度"):
        _provider(_Transport()).generate([Message("user", "hi")], model="gemini-4-argon@max")


def test_trailing_assistant_prefill_rejected_on_gemini4():
    with pytest.raises(ValueError, match="预填充"):
        _provider(_Transport()).generate(
            [Message("user", "hi"), Message("assistant", "```draft\n")],
            model="gemini-4-argon",
        )


def test_missing_api_key_rejected():
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiProvider(api_key=None)


def test_safety_threshold_applied_to_all_categories():
    t = _Transport([_chunk("ok", finish="STOP")])
    _provider(t, safety_threshold="block_only_high").generate(
        [Message("user", "hi")], model="gemini-4-argon",
    )
    settings = t.calls[0]["body"]["safetySettings"]
    assert len(settings) == 4
    assert {s["threshold"] for s in settings} == {"BLOCK_ONLY_HIGH"}
    with pytest.raises(ValueError, match="安全阈值"):
        GeminiProvider(api_key="k", safety_threshold="LOOSE")


# ── long decode 续接 ──────────────────────────────────────────────────────────

def test_continuation_hops_are_stitched_and_billed():
    t = _Transport(
        [
            _chunk("第一跳", thought="（思考摘要不进正文）"),
            _chunk("……", finish="CONTINUATION", token="tok-1", usage={
                "promptTokenCount": 1000, "cachedContentTokenCount": 0,
                "candidatesTokenCount": 65_000, "thoughtsTokenCount": 500,
            }),
        ],
        [
            _chunk("第二跳", finish="STOP", usage={
                "promptTokenCount": 1000, "cachedContentTokenCount": 950,
                "candidatesTokenCount": 2000, "thoughtsTokenCount": 0,
            }),
        ],
    )
    resp = _provider(t).generate(
        [Message("user", "写一整卷")], model="gemini-4-argon@medium", max_tokens=200_000,
    )
    assert resp.text == "第一跳……第二跳"
    assert resp.stop_reason == "end_turn"
    assert len(resp.raw["hops"]) == 2
    first, second = (c["body"] for c in t.calls)
    # 续接：原样重发原始请求 + 顶层 continuationToken，maxOutputTokens 不变
    assert second.pop("continuationToken") == "tok-1"
    assert second == first
    assert resp.usage.input == 2000
    assert resp.usage.output == 65_000 + 500 + 2000
    assert resp.usage.cache_read == 950
    assert resp.usage.provider == "gemini"


def test_thinking_exhausted_budget_is_extended_once():
    t = _Transport(
        [_chunk(None, finish="MAX_TOKENS", token="tok-1")],
        [_chunk('{"ok": true}', finish="STOP")],
    )
    resp = _provider(t).generate(
        [Message("user", "只输出 JSON")], model="gemini-4-argon@low", max_tokens=400,
    )
    assert resp.text == '{"ok": true}'
    first, second = (c["body"] for c in t.calls)
    assert second["continuationToken"] == "tok-1"
    assert (second["generationConfig"]["maxOutputTokens"]
            == 2 * first["generationConfig"]["maxOutputTokens"])


def test_max_tokens_with_visible_text_is_truncation():
    t = _Transport([_chunk("半截", finish="MAX_TOKENS", token="tok-1")])
    resp = _provider(t).generate([Message("user", "hi")], model="gemini-4-argon", max_tokens=10)
    assert resp.text == "半截"
    assert resp.stop_reason == "max_tokens"
    assert len(t.calls) == 1


def test_prompt_block_maps_to_refusal():
    t = _Transport([{"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}])
    resp = _provider(t).generate([Message("user", "hi")], model="gemini-4-argon")
    assert resp.text == ""
    assert resp.stop_reason == "refusal"
    assert resp.raw["block_reason"] == "PROHIBITED_CONTENT"


# ── 工具调用 ──────────────────────────────────────────────────────────────────

def test_tools_map_to_function_declarations_and_calls():
    t = _Transport([_chunk(call={"id": "c1", "name": "lookup", "args": {"q": "宗门"}},
                           finish="STOP")])
    tool = Tool("lookup", "查设定", {"type": "object", "properties": {"q": {"type": "string"}}})
    resp = _provider(t).generate([Message("user", "查")], model="gemini-4-argon", tools=[tool])
    decl = t.calls[0]["body"]["tools"][0]["functionDeclarations"][0]
    assert decl["name"] == "lookup"
    assert decl["parametersJsonSchema"]["properties"]["q"]["type"] == "string"
    assert resp.stop_reason == "tool_use"
    assert resp.tool_calls[0].tool_use_id == "c1"
    assert resp.tool_calls[0].input == {"q": "宗门"}


# ── 默认 urllib transport：SSE 解析与错误映射 ─────────────────────────────────

class _FakeHTTPResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_default_transport_parses_sse(monkeypatch):
    events = [_chunk("你"), _chunk("好", finish="STOP", usage={"promptTokenCount": 3})]
    payload = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\r\n\r\n" for e in events)
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["key"] = req.get_header("X-goog-api-key")
        seen["timeout"] = timeout
        return _FakeHTTPResponse(payload.encode("utf-8"))

    monkeypatch.setattr(gp.urllib.request, "urlopen", fake_urlopen)
    resp = GeminiProvider(api_key="k", base_url="https://proxy.example/").generate(
        [Message("user", "hi")], model="gemini-4-argon",
    )
    assert resp.text == "你好"
    assert resp.usage.input == 3
    assert seen["url"].startswith("https://proxy.example/v1beta/models/gemini-4-argon:")
    assert seen["key"] == "k"
    assert seen["timeout"] == 20 * 60


def test_default_transport_maps_http_error(monkeypatch):
    body = json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                 "message": "quota"}}).encode()

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, io.BytesIO(body))

    monkeypatch.setattr(gp.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(GeminiAPIError, match="429 RESOURCE_EXHAUSTED: quota"):
        GeminiProvider(api_key="k").generate([Message("user", "hi")], model="gemini-4-argon")


def test_gateway_retries_rate_limited_gemini_call(monkeypatch):
    import novelforge.control_plane.llm.gateway as gateway_mod

    monkeypatch.setattr(gateway_mod.time, "sleep", lambda s: None)
    t = _Transport(
        GeminiAPIError(429, "RESOURCE_EXHAUSTED", "slow down"),
        [_chunk("ok", finish="STOP")],
    )
    gw = LLMGateway(_provider(t), BudgetLedger(max_tokens=10**7, max_usd=100.0),
                    model_map=dict(GEMINI_MODELS))
    assert gw.generate(ModelTier.FAST, [Message("user", "hi")]).text == "ok"
    assert len(t.calls) == 2


# ── 能力 / 计费 ────────────────────────────────────────────────────────────────

def test_capabilities_report_unified_million_token_window():
    p = GeminiProvider(api_key="k")
    caps = p.capabilities("gemini-4-argon@high")
    assert caps.long_output is True
    assert caps.max_tokens_out == caps.context_window == 1_048_576
    other = p.capabilities("gemini-3.8-flash")
    assert other.long_output is False
    assert other.max_tokens_out == 65_536


def test_pricing_strips_thinking_suffix_and_discounts_implicit_cache():
    assert _estimate_cost(1_000_000, 0, "gemini-4-argon@high") == pytest.approx(4.0)
    assert _estimate_cost(0, 1_000_000, "gemini-4-argon@low") == pytest.approx(20.0)
    assert _estimate_cost(1_000_000, 0, "gemini-4-argon", cached_in=1_000_000) == pytest.approx(0.2)
    # 未登记缓存价的模型保持旧口径
    assert _estimate_cost(1_000_000, 0, "claude-sonnet-4-6", cached_in=1_000_000) == pytest.approx(3.0)

    ledger = BudgetLedger(max_tokens=10**7, max_usd=100.0)
    ledger.charge(Usage(input=1_000_000, output=0, cache_read=500_000, model="gemini-4-argon@high"))
    assert ledger.usd_spent == pytest.approx(0.5 * 4.0 + 0.5 * 0.2)
    assert ledger.cache_read_tokens == 500_000


# ── 工厂 / 运行时接线 ──────────────────────────────────────────────────────────

def test_factory_maps_tiers_to_thinking_levels():
    cfg = NovelForgeConfig()
    cfg.provider = ProviderConfig(provider="gemini", api_key="k")
    gw = build_gateway(cfg)
    assert gw.model_for(ModelTier.FAST) == "gemini-4-argon@low"
    assert gw.model_for(ModelTier.MID) == "gemini-4-argon@medium"
    assert gw.model_for(ModelTier.STRONG) == "gemini-4-argon@high"

    cfg.provider.fast_model = "gemini-3.8-flash"
    assert build_gateway(cfg).model_for(ModelTier.FAST) == "gemini-3.8-flash"


@pytest.mark.parametrize("requested", ["gemini", "google", "GEMINI"])
def test_explicit_gemini_uses_gemini_key(clean_env, requested):
    clean_env.setenv("NOVELFORGE_PROVIDER", requested)
    clean_env.setenv("GEMINI_API_KEY", "gemini-key")
    clean_env.setenv("DEEPSEEK_API_KEY", "wrong-key")
    cfg = build_project_config("p", "novel.db")
    assert cfg.provider.provider == "gemini"
    assert cfg.provider.api_key == "gemini-key"


def test_explicit_gemini_accepts_google_api_key(clean_env):
    clean_env.setenv("NOVELFORGE_PROVIDER", "gemini")
    clean_env.setenv("GOOGLE_API_KEY", "google-key")
    assert build_project_config("p", "novel.db").provider.api_key == "google-key"


def test_gemini_key_wins_provider_inference(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "gemini-key")
    clean_env.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    cfg = build_project_config("p", "novel.db")
    assert cfg.provider.provider == "gemini"
    assert cfg.provider.api_key == "gemini-key"


def test_generic_google_key_does_not_drive_inference(clean_env):
    clean_env.setenv("GOOGLE_API_KEY", "maps-key")
    cfg = build_project_config("p", "novel.db")
    assert cfg.provider.provider != "gemini"
    assert cfg.provider.api_key is None


def test_gemini_env_overrides_endpoint_safety_and_tiers(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "gemini-key")
    clean_env.setenv("GEMINI_BASE_URL", "https://gemini-proxy.example")
    clean_env.setenv("NOVELFORGE_GEMINI_SAFETY_THRESHOLD", "BLOCK_ONLY_HIGH")
    clean_env.setenv("NOVELFORGE_STRONG_MODEL", "gemini-4-argon@medium")
    cfg = build_project_config("p", "novel.db")
    assert cfg.provider.base_url == "https://gemini-proxy.example"
    assert cfg.provider.safety_threshold == "BLOCK_ONLY_HIGH"
    # 显式外部 embedding 端点不能被 Gemini 代理地址污染
    assert cfg.memory.embedding_base_url is None
    gw = build_project_gateway("p", "novel.db")
    assert gw.model_for(ModelTier.STRONG) == "gemini-4-argon@medium"
    assert gw.model_for(ModelTier.FAST) == "gemini-4-argon@low"


def test_gemini_without_key_degrades_to_none(clean_env):
    clean_env.setenv("NOVELFORGE_PROVIDER", "gemini")
    assert build_project_gateway("p", "novel.db") is None
    assert build_project_gateway("p", "novel.db", allow_missing=True) is None


def test_legacy_explicit_key_keeps_resolved_provider(clean_env):
    cfg = NovelForgeConfig()
    apply_provider_env(cfg, api_key="legacy")
    assert (cfg.provider.provider, cfg.provider.api_key) == ("deepseek", "legacy")

    clean_env.setenv("NOVELFORGE_PROVIDER", "fake")
    cfg = NovelForgeConfig()
    apply_provider_env(cfg, api_key="legacy")
    assert (cfg.provider.provider, cfg.provider.api_key) == ("fake", None)

    clean_env.setenv("NOVELFORGE_PROVIDER", "gemini")
    cfg = NovelForgeConfig()
    apply_provider_env(cfg, api_key="legacy")
    assert (cfg.provider.provider, cfg.provider.api_key) == ("gemini", "legacy")


def test_planning_horizon_follows_long_output_capability(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "gemini-key")
    assert max_planning_horizon(build_project_config("p", "novel.db")) == LONG_OUTPUT_PLANNING_HORIZON
    clean_env.setenv("NOVELFORGE_PROVIDER", "fake")
    assert max_planning_horizon(build_project_config("p", "novel.db")) == DEFAULT_PLANNING_HORIZON


# ── 端到端：卷规划在 Gemini 4 下单次覆盖 30 章 ───────────────────────────────

@pytest.fixture
def gemini_client(tmp_path, clean_env):
    clean_env.setenv("NOVELFORGE_DATA", str(tmp_path))
    clean_env.setenv("NOVELFORGE_PROVIDER", "gemini")
    clean_env.setenv("GEMINI_API_KEY", "gemini-key")
    import novelforge.app.deps as deps_mod
    from fastapi.testclient import TestClient
    from novelforge.app.main import app

    deps_mod._registry = None
    with TestClient(app) as c:
        yield c
    deps_mod._registry = None


def test_volume_plan_uses_long_window_on_gemini(gemini_client, clean_env):
    requests: list[dict] = []

    def fake_transport(url, headers, body, timeout):
        requests.append(body)
        prompt = body["contents"][0]["parts"][0]["text"]
        items = []
        if "规划范围" in prompt:
            items = [{
                "chapter": ch, "title": f"第{ch}章", "goal": f"第{ch}章目标：主角破局",
                "hook_text": f"第{ch}章末悬念",
                "beats": [
                    {"beat_type": "setup", "summary": "铺垫", "value_axis": "平静→紧张"},
                    {"beat_type": "turn", "summary": "转折", "value_axis": "紧张→危机"},
                    {"beat_type": "hook", "summary": "钩子", "value_axis": "危机→悬念"},
                ],
            } for ch in range(1, 31)]
        text = "```plans\n" + json.dumps(items, ensure_ascii=False) + "\n```"
        return iter([_chunk(text, finish="STOP")])

    clean_env.setattr(gp, "_urllib_transport", fake_transport)
    client = gemini_client
    project = client.post("/v1/projects", json={"name": "长卷", "genre": "xuanhuan"}).json()["project_id"]
    assert client.post(f"/v1/{project}/volumes", json={
        "volume_no": 1, "title": "宗门", "synopsis": "外门到内门",
        "start_chapter": 1, "end_chapter": 60,
    }).status_code == 201

    body = client.post(f"/v1/{project}/volumes/1/plan", json={"to_chapter": 60}).json()
    assert body["error"] is None, body
    assert body["to_chapter"] == LONG_OUTPUT_PLANNING_HORIZON
    assert [c["chapter"] for c in body["planned"]] == list(range(1, 31))
    assert body["planned"][29]["title"] == "第30章"      # 模型产出，而非确定性兜底
    plan_calls = [r for r in requests if "规划范围" in r["contents"][0]["parts"][0]["text"]]
    assert len(plan_calls) == 1
    cfg = plan_calls[0]["generationConfig"]
    assert cfg["thinkingConfig"] == {"thinkingLevel": "high"}
    assert cfg["maxOutputTokens"] == 30 * 1024 + 32_768
