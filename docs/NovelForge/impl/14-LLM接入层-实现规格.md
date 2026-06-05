# LLM接入层 实现规格 (§14)（实现规格）

> 本篇把设计稿 §14（LLM 接入层）展开为**实现级规格**：精确到可直接照着写 Python——类/函数签名（带类型注解）、关键函数体伪代码（接近真实可运行）、异常处理与重试语义、边界情况逐条枚举、Mermaid 时序图、模块文件布局、pytest 测试计划。
>
> **接缝纪律（铁律）**：本篇所有归一化类型（`ChatMessage`/`Tool`/`ToolCall`/`ToolResult`/`Response`/`Usage`/`Pricing`/`CacheHint`/`ProviderStreamEvent`/`CapabilitySet`/`ModelTier`/异常树）**一律从接缝四件套 `import`，绝不重定义**。权威定义见 `D:\work\zi\docs\NovelForge\impl\00-接缝契约与文件布局.md`（下称「接缝篇」）。本篇只实现 §14 接入层模块：`novelforge/llm/{provider,gateway,pricing,cache,structured,stream,tool_fallback,config}.py` + `novelforge/llm/providers/{__init__,fake,anthropic,openai_compat,local}.py`。
>
> **取代设计稿示意路径**：设计稿 §14 出现的 `control_plane/llm/*` 一律改用 `novelforge/llm/*` 包路径。
>
> **运行环境（实测）**：Python 3.13；已装 `pydantic`/`pyyaml`/`pytest`；**未装** `anthropic`/`openai`/`httpx`。因此三家真实 Provider 的 SDK/`httpx` 一律**可选依赖**：能力探测 + 缺失即降级/跳过；零外网、零 SDK、零 key 即可 `import` 与单测（`FakeProvider` + in-memory sqlite）。

---

## 14.0 模块职责与依赖（实现总览）

接入层四层（设计 §14.1）落到 `novelforge/llm/` 下的物理模块。依赖方向严格单向（接缝篇 §6）：`llm/*` **绝不** `import` `novelforge.tools` / `novelforge.skills`；只 `import` 接缝四件套与 `db/`（`structured.py` 产 `BibleChangeProposal` 需 `contracts.py`）。

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `llm/types.py`（接缝） | 归一化类型权威定义 | `ChatMessage/Tool/ToolCall/ToolResult/Response/Usage/Pricing/CacheHint/ProviderStreamEvent/CapabilitySet/Role/StopReason/StreamEventType` + `LLMUsage` 别名 |
| `llm/errors.py`（接缝） | 异常树 | `LLMError/ProviderError/RateLimitError/ServerError/CapabilityUnsupported/StructuredOutputError/ToolProtocolError/AllProvidersFailed` |
| `llm/tiers.py`（接缝） | 语义档 | `ModelTier/normalize_tier` |
| `llm/provider.py` | `LLMProvider` 协议（无状态适配器） | `LLMProvider` |
| `llm/config.py` | `config.providers` 段加载与校验；key 仅 env | `ProvidersConfig/ProviderConfig/load_providers_config` |
| `llm/pricing.py` | 定价装载 + 各厂商 usage→`Usage` 归一化映射 | `pricing_for/normalize_anthropic_usage/normalize_openai_usage/normalize_ollama_usage` |
| `llm/cache.py` | `CacheHint` 渲染纪律（稳定前缀不污染断言） | `render_prompt/has_volatile_token` |
| `llm/tool_fallback.py` | 无原生 tool-use 的提示式 JSON 协议 render/parse | `render_tool_protocol/parse_tool_protocol/extract_first_json_object` |
| `llm/structured.py` | `generate_structured` instructor 式校验-修复重试 | `generate_structured` |
| `llm/stream.py` | `ProviderStreamEvent` 装配工具（增量→完整 `ToolCall`） | `StreamAssembler` |
| `llm/gateway.py` | 档→model 映射 / 降级 / 退避重试 / 回退链 / 记账 / 缓存验证 | `LLMGateway/degrade_plan/resolve_model/classify_error/sleep_backoff` |
| `llm/providers/__init__.py` | `build_providers(cfg)` 工厂（按 type 选实现，缺 SDK→跳过） | `build_providers` |
| `llm/providers/fake.py` | `FakeProvider`（测试核心，零外网） | `FakeProvider` |
| `llm/providers/anthropic.py` | `AnthropicProvider`（`import anthropic` 可选） | `AnthropicProvider` |
| `llm/providers/openai_compat.py` | `OpenAICompatProvider`（OpenAI/vLLM/网关；`import openai` 可选） | `OpenAICompatProvider` |
| `llm/providers/local.py` | `LocalProvider`（ollama/llama.cpp；`httpx` 可选；无原生 tool→兜底） | `LocalProvider` |

> **字段以各厂商官方最新文档为准**：本篇给出的 Anthropic（`input_schema`/`tools`/`cache_control`/`usage.cache_read_input_tokens`/SSE `content_block_delta`）、OpenAI（`tools`/`response_format`/`stream_options.include_usage`/`usage`）、ollama（`/api/chat` 的 `tools`/`format`/`message.tool_calls`/`eval_count`）字段名/模型 ID/定价均为**工程结构示意**。落地时**可用 context7 核对**各厂商最新请求与 usage 字段（`resolve-library-id` → `query-docs`，对 `anthropic` / `openai` / `ollama`）。`LLMProvider` 的职责就是把这些易变细节封死在适配层内。

---

## 14.1 接缝类型引用（不重定义）

本篇所有模块统一从接缝四件套引入。下文伪代码中所有未在本篇定义 `class` 的类型，均来自这些 import：

```python
# 任意 novelforge/llm/*.py 头部
from __future__ import annotations
from novelforge.llm.types import (
    Role, StopReason, StreamEventType,
    ContentPart, ChatMessage, CostHint, Tool, ToolCall, ToolResult,
    Pricing, Usage, CacheHint, Response, ProviderStreamEvent, CapabilitySet,
    LLMUsage,                                   # = Usage 别名（§07.2.2 兼容）
)
from novelforge.llm.tiers import ModelTier, normalize_tier
from novelforge.llm.errors import (
    LLMError, ProviderError, RateLimitError, ServerError,
    CapabilityUnsupported, StructuredOutputError, ToolProtocolError, AllProvidersFailed,
)
```

> 接缝硬约束：本篇**不出现任何** `class ChatMessage(...)` / `class Usage(...)` / `class ToolCall(...)` 等接缝类型的 `class` 定义。`Usage.usd(pricing)` 取代设计稿 §14.8 的游离函数 `cost_usd(u, p)`；`Usage.billable()` 取代 §07.2.2 `LLMUsage.billable_tokens()`。`Tool.json_schema` 取代设计稿 `parameters`/`input_schema`；`ToolCall.args` 取代 `arguments`。

---

## 14.2 `provider.py`：LLMProvider 协议（无状态适配器）

`LLMProvider` 是接缝篇 §2.1 已定签名的协议——本篇只**重申约束并给实现纪律**，不重定义协议本身（协议类已在接缝篇定义，本模块仅 `import` 转出 + 提供 mixin 工具）。

```python
# novelforge/llm/provider.py
from __future__ import annotations
from typing import Protocol, Iterator, runtime_checkable
from novelforge.llm.types import (
    ChatMessage, Tool, Response, CapabilitySet, CacheHint, ProviderStreamEvent,
)


@runtime_checkable
class LLMProvider(Protocol):
    """无状态厂商适配器：只做「翻译进、归一化出」。
    不持有预算/不重试/不记账/不做能力降级——全在 LLMGateway（设计 §14.1.1）。"""
    name: str

    def capabilities(self, model: str) -> CapabilitySet: ...

    def generate(
        self, *,
        messages: list[ChatMessage], model: str,
        system: str | None = None, tools: list[Tool] | None = None,
        response_schema: dict | None = None, cache_hint: CacheHint | None = None,
        stream: bool = False, max_tokens: int = 4096,
    ) -> Response: ...

    def stream(
        self, *,
        messages: list[ChatMessage], model: str,
        system: str | None = None, tools: list[Tool] | None = None,
        response_schema: dict | None = None, cache_hint: CacheHint | None = None,
        max_tokens: int = 4096,
    ) -> Iterator[ProviderStreamEvent]: ...
```

实现纪律（每个具体 Provider 必守）：
1. **无跨调用状态**：`__init__` 只存 `base_url`/`api_key`/`caps_decl`/SDK client；不缓存上一次响应。
2. **SDK 可选**：`import anthropic` / `import openai` / `import httpx` 包在 `try/except ImportError` 内；缺失时**模块仍可 import**，仅在 `__init__` 构造或首次调用时抛 `LLMError("provider X requires SDK ...")`。
3. **`raw` 不入库不入日志**：`Response.raw` 仅置原始响应供调试；落 `tool_call_log`/`skill_run_log` 前由 Gateway 剥离（§14.9 红线）。
4. **错误归一**：捕获厂商异常 → 抛 `ProviderError(error_class=..., provider=self.name, status_code=...)`（映射表见 §14.10）。Provider **不重试、不回退**。

---

## 14.3 `config.py`：providers 段加载与校验（Pydantic settings，key 仅 env）

承接设计 §14.9。Pydantic v2 模型 + `pyyaml` 装载；**API key 仅从环境变量读，绝不入库、不入日志、不入 `Response.raw`**。

```python
# novelforge/llm/config.py
from __future__ import annotations
import os
from typing import Literal
from pydantic import BaseModel, Field, model_validator
from novelforge.llm.types import Pricing, CapabilitySet
from novelforge.llm.tiers import ModelTier

ProviderType = Literal["anthropic", "openai_compat", "local_ollama", "fake"]


class RetryConfig(BaseModel):
    max_attempts: int = 5
    base_ms: int = 500
    max_ms: int = 30000
    jitter: bool = True


class TimeoutConfig(BaseModel):
    connect_s: int = 10
    read_s: int = 600


class ProviderConfig(BaseModel):
    """单供应商 config（设计 §14.9 providers.<id>）。"""
    type: ProviderType
    base_url: str = ""
    api_key_env: str | None = None              # 仅 env 变量名；值绝不入此对象
    models: dict[str, str]                       # {"fast"/"mid"/"strong": model_id}
    capabilities: CapabilitySet = Field(default_factory=CapabilitySet)
    pricing: dict[str, Pricing] = Field(default_factory=dict)   # {"fast"/...: Pricing}
    timeout: TimeoutConfig = Field(default_factory=TimeoutConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)

    @model_validator(mode="after")
    def _check_tiers(self) -> "ProviderConfig":
        # 三档 model 必须齐全（别名 haiku/sonnet/opus 归一到 fast/mid/strong）
        need = {"fast", "mid", "strong"}
        have = {ModelTier(k).value for k in self.models}   # 归一别名
        missing = need - have
        assert not missing, f"provider models missing tiers: {missing}"
        # pricing 缺省→本地零价（local 允许空 pricing）
        for t in need:
            self.pricing.setdefault(t, Pricing())
        return self

    def resolve_key(self) -> str | None:
        """运行时取 key；仅在真实调用时调用。绝不把返回值写入任何持久层。"""
        if not self.api_key_env:
            return None
        key = os.environ.get(self.api_key_env)
        # 不抛明文；只抛变量名（信息泄露安全）
        assert key, f"env var {self.api_key_env} not set (api key required)"
        return key


class ProvidersConfig(BaseModel):
    """config.providers 段根（设计 §14.9）。"""
    default: str                                 # 主供应商 id
    fallback: list[str] = Field(default_factory=list)  # 回退链 id 序列
    providers: dict[str, ProviderConfig]         # id → ProviderConfig

    @model_validator(mode="after")
    def _check_chain(self) -> "ProvidersConfig":
        assert self.default in self.providers, f"default '{self.default}' not in providers"
        for pid in self.fallback:
            assert pid in self.providers, f"fallback '{pid}' not in providers"
        return self

    def chain(self) -> list[str]:
        """主→回退顺序（去重保序）。"""
        seen, out = set(), []
        for pid in [self.default, *self.fallback]:
            if pid not in seen:
                seen.add(pid); out.append(pid)
        return out


def load_providers_config(raw: dict) -> ProvidersConfig:
    """从 yaml 解析出的 dict 装载（pyyaml 读文件在调用方）。"""
    return ProvidersConfig.model_validate(raw["providers"])
```

**key 管理硬纪律（实现断言）**：
- `ProviderConfig` 对象内**只存 `api_key_env`（变量名）**，从不存 key 值。`resolve_key()` 是唯一取值点，仅在真实 SDK 调用前调用。
- `ProvidersConfig.model_dump()` 中不含任何 key 值（因为对象里就没有）——可安全序列化进 `skill_run_log`/审计（但通常也不入库）。
- 测试 `test_config_key_only_env_name`：断言 `ProviderConfig` 字段集不含 `api_key`/`api_secret` 等；只有 `api_key_env`。

---

## 14.4 `pricing.py`：定价装载 + 各厂商 usage→Usage 归一化

承接设计 §14.8。`Usage.usd(pricing)`（接缝方法）做计价；本模块只负责**各厂商原始 usage 字段 → 归一化 `Usage` 的映射**（精确到字段）。

```python
# novelforge/llm/pricing.py
from __future__ import annotations
from typing import Any
from novelforge.llm.types import Usage, Pricing
from novelforge.llm.tiers import ModelTier
from novelforge.llm.config import ProviderConfig


def pricing_for(pc: ProviderConfig, tier: ModelTier) -> Pricing:
    """档→该供应商该档定价（别名归一）。"""
    return pc.pricing[ModelTier(tier.value).value]


def normalize_anthropic_usage(raw: Any, *, provider: str, model: str) -> Usage:
    """Anthropic usage 字段 → Usage（字段以官方最新文档为准；可用 context7 核对 anthropic）。
    input_tokens / output_tokens / cache_read_input_tokens / cache_creation_input_tokens。"""
    g = _getter(raw)
    return Usage(
        input=g("input_tokens", 0),
        output=g("output_tokens", 0),
        cache_read=g("cache_read_input_tokens", 0),
        cache_write=g("cache_creation_input_tokens", 0),
        provider=provider, model=model,
    )


def normalize_openai_usage(raw: Any, *, provider: str, model: str) -> Usage:
    """OpenAI 兼容 usage → Usage（字段以官方/网关文档为准；可用 context7 核对 openai）。
    prompt_tokens / completion_tokens；缓存命中 prompt_tokens_details.cached_tokens。
    归一：input = prompt − cached，cache_read = cached（避免重复计费 token）。"""
    g = _getter(raw)
    prompt = g("prompt_tokens", 0)
    completion = g("completion_tokens", 0)
    details = g("prompt_tokens_details", {}) or {}
    cached = (details.get("cached_tokens", 0) if isinstance(details, dict)
              else _getter(details)("cached_tokens", 0))
    return Usage(
        input=max(prompt - cached, 0),
        output=completion,
        cache_read=cached,
        cache_write=0,                  # OpenAI 自动缓存无显式写计费
        provider=provider, model=model,
    )


def normalize_ollama_usage(raw: dict, *, provider: str, model: str) -> Usage:
    """ollama /api/chat usage → Usage（字段以官方文档为准；可用 context7 核对 ollama）。
    prompt_eval_count / eval_count；本地无缓存字段 → cache_read=0。"""
    return Usage(
        input=raw.get("prompt_eval_count", 0),
        output=raw.get("eval_count", 0),
        cache_read=0, cache_write=0,
        provider=provider, model=model,
    )


def _getter(obj: Any):
    """SDK 对象（属性）/ dict（键）统一访问器。"""
    if isinstance(obj, dict):
        return lambda k, d=None: obj.get(k, d)
    return lambda k, d=None: getattr(obj, k, d)
```

各厂商 usage 字段 → 归一 `Usage` 对照（设计 §14.8 表）：

| 供应商 | 原始字段 | → `Usage` |
|---|---|---|
| Anthropic | `usage.input_tokens` / `output_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens` | `input` / `output` / `cache_read` / `cache_write` |
| OpenAI 兼容 | `usage.prompt_tokens` / `completion_tokens` / `prompt_tokens_details.cached_tokens` | `input`=prompt−cached / `output` / `cache_read`=cached / `cache_write`=0 |
| ollama 原生 | `prompt_eval_count` / `eval_count` | `input` / `output`（`cache_read`=0） |

记账落库：Gateway 用 `usage.usd(pricing_for(pc, tier))` 算美元，`usage.billable()` 算 token 维度，喂 §07.6 `BudgetLedger.charge(usage)`（`LLMUsage = Usage` 别名，`charge` 内调 `usage.billable()` / `usage.usd(...)`）。审计写 `tool_call_log.provider`/`model`（接缝篇引用 §12.5 DDL：列名 `provider`/`model`/`note`），`skill_run_log` 的 `prompt_tokens`/`cache_read_tokens`/`output_tokens`/`usd_cost`（§07.3.1 真实列名）由 Skill 层落。

---

## 14.5 `cache.py`：prompt 缓存归一化（稳定前缀不污染，HP10）

承接设计 §14.6。三类缓存（Anthropic `explicit` / OpenAI `auto` / 本地 `none`），但**「稳定前缀不被污染」纪律全供应商一致**。本模块给统一渲染纪律 + 污染断言；具体 `cache_control` 断点/字节稳定由各 Provider 翻译 `CacheHint`。

```python
# novelforge/llm/cache.py
from __future__ import annotations
import re
from novelforge.llm.types import CacheHint

# 易变 token 模式：章节号 / uuid / ISO 时间戳 / "as_of" 等（稳定前缀里出现即污染）
_VOLATILE = re.compile(
    r"(第\s*\d+\s*章|chapter[_\s-]?\d+|as[_\s-]?of|"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}|"                       # uuid 片段
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2})",                      # ISO 时间戳
    re.IGNORECASE,
)


def has_volatile_token(block: str) -> bool:
    return bool(_VOLATILE.search(block or ""))


def render_prompt(hint: CacheHint | None, dynamic_suffix: str) -> tuple[list[str], str]:
    """统一渲染纪律（全供应商执行，承接 §07.5 不变量2/§08.3 缓存红线）。
    返回 (stable_blocks, dynamic_suffix)：dynamic 永远拼在最后断点之后。
    不变量：stable 内不得含章节号/时间戳/uuid/检索结果（HP10）。"""
    stable = list(hint.stable_blocks) if hint else []
    polluted = [b[:40] for b in stable if has_volatile_token(b)]
    assert not polluted, f"stable prefix polluted (HP10): {polluted}"
    return stable, dynamic_suffix
```

| 供应商 | 缓存类型 | Provider 翻译 `CacheHint` 的动作 | 命中验证字段（以官方文档为准） |
|---|---|---|---|
| Anthropic（`explicit`） | 在 `stable_blocks` 末尾（system/tools 末块）打 `cache_control={"type":"ephemeral","ttl":hint.ttl}` | `usage.cache_read_input_tokens`（>0=命中）/ `cache_creation_input_tokens`（写入） |
| OpenAI 兼容（`auto`） | 不打断点，仅保证前缀字节稳定（顺序固定、dynamic 后置） | `usage.prompt_tokens_details.cached_tokens`（命名随网关，以文档为准） |
| 本地（`none`） | no-op；仍按稳定前缀渲染（前缀纪律全供应商一致） | 无缓存字段；`Usage.cache_read=0` |

**silent invalidator 告警**（Gateway 内）：同一稳定前缀的重复请求若 `caps.prompt_caching != "none"` 且 `usage.cache_read == 0` 且非首调，记一条 `log.warning("prefix cache MISS — 检查前缀是否被章节号/uuid/检索结果污染")`（同 §07.6.2）。本地供应商 `prompt_caching=="none"` 时**抑制告警**（`cache_read=0` 属正常）。

---

## 14.6 `tool_fallback.py`：提示式 JSON 工具协议（无原生 tool-use 兜底）

承接设计 §14.3.2。当 `caps.tool_use == False`（如旧 llama.cpp 模型），把工具清单注入 prompt，约束模型只输出工具调用 JSON，解析回 `ToolCall`。产出 `ToolCall` 与原生路径**形态完全一致**——§12 工具循环无需分支。

```python
# novelforge/llm/tool_fallback.py
from __future__ import annotations
import json
from novelforge.llm.types import Tool, ToolCall
from novelforge.ids import new_id          # 复用已落地 ids.py（new_id(prefix)）

TOOL_PROTOCOL_SYSTEM = """你可调用以下只读工具补取上下文。需要调用时，只输出一个 JSON：
{{"tool":"<name>","arguments":{{...}}}}
不要输出任何其它文字。不需要工具时输出：{{"tool":null,"answer":"<最终答复>"}}
可用工具:
{tool_specs}"""


def render_tool_protocol(tools: list[Tool]) -> str:
    """工具清单 → 注入 system 的协议说明。json_schema 即接缝 Tool 的入参契约。"""
    specs = "\n".join(
        f"- {t.name}: {t.description}\n  参数 schema: "
        f"{json.dumps(t.json_schema, ensure_ascii=False)}"
        for t in tools
    )
    return TOOL_PROTOCOL_SYSTEM.format(tool_specs=specs)


def extract_first_json_object(text: str) -> dict | None:
    """容错抽取首个合法 JSON 对象：剥 ```json fence / 前后噪声 / 花括号配平扫描。"""
    if not text:
        return None
    s = text.strip()
    # 剥 code fence
    if "```" in s:
        for part in s.split("```"):
            p = part.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            cand = _try_braces(p)
            if cand is not None:
                return cand
    return _try_braces(s)


def _try_braces(s: str) -> dict | None:
    start = s.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': in_str = False
            continue
        if c == '"': in_str = True
        elif c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def parse_tool_protocol(text: str) -> tuple[list[ToolCall], str]:
    """从模型文本抽出工具调用 JSON。
    返回 (tool_calls, answer)：tool=null → 空 calls + answer；解析失败 → 空 calls + 原文（触发上层修复）。"""
    obj = extract_first_json_object(text)
    if not obj or obj.get("tool") in (None, "null"):
        return [], (obj or {}).get("answer", text)
    return [ToolCall(id=new_id("tc"),
                     name=obj["tool"],
                     args=obj.get("arguments", {}) or {})], ""
```

兜底失败语义：连续解析不出合法 JSON → Gateway 按 §14.4 修复重试上限处理；超限 → 该步降级为「无工具直接生成」并标 `degraded`（写 `tool_call_log.note="degraded"`）。因 NovelForge 受限工具是只读 SQL（HP1），兜底偶发失败最坏只是少补一次上下文，由后续 Check 兜底，不污染 canon。

---

## 14.7 `stream.py`：流式归一化与装配

承接设计 §14.7。统一 `ProviderStreamEvent`（接缝篇 §1.8），经 Orchestrator 转译为 §13 业务级 SSE。本模块给**增量装配器**：把 `tool_call_delta` 序列拼成完整 `ToolCall`，把 `text_delta` 累计成最终 text，末尾产 `usage`。

```python
# novelforge/llm/stream.py
from __future__ import annotations
import json
from typing import Iterator
from novelforge.llm.types import (
    ProviderStreamEvent, StreamEventType, ToolCall, Usage, Response, StopReason,
)
from novelforge.ids import new_id


class StreamAssembler:
    """把 ProviderStreamEvent 序列装配为最终 Response（供 generate(stream=True) 内部转调）。
    各 Provider 的 stream() 只需 yield 归一化事件；装配逻辑全供应商共享。"""

    def __init__(self) -> None:
        self._text_parts: list[str] = []
        self._tool_buf: dict[str, dict] = {}     # id → {"name":..., "args_str":...}
        self._tool_order: list[str] = []
        self._usage = Usage()
        self._stop: StopReason = StopReason.STOP

    def feed(self, ev: ProviderStreamEvent) -> None:
        if ev.type == StreamEventType.TEXT_DELTA and ev.text:
            self._text_parts.append(ev.text)
        elif ev.type == StreamEventType.TOOL_CALL_DELTA and ev.partial_args is not None:
            # 增量 JSON 片段累计（OpenAI: 按 index 累计 arguments 串；Anthropic: input_json_delta）
            tc = ev.tool_call
            tid = (tc.id if tc and tc.id else (self._tool_order[-1] if self._tool_order else new_id("tc")))
            slot = self._tool_buf.setdefault(tid, {"name": "", "args_str": ""})
            if tid not in self._tool_order:
                self._tool_order.append(tid)
            if tc and tc.name:
                slot["name"] = tc.name
            slot["args_str"] += ev.partial_args
        elif ev.type == StreamEventType.TOOL_CALL_DONE and ev.tool_call:
            tc = ev.tool_call
            self._tool_buf[tc.id] = {"name": tc.name, "args_str": json.dumps(tc.args)}
            if tc.id not in self._tool_order:
                self._tool_order.append(tc.id)
            self._stop = StopReason.TOOL_USE
        elif ev.type in (StreamEventType.USAGE, StreamEventType.DONE) and ev.usage:
            self._usage = ev.usage
            if ev.stop_reason:
                self._stop = ev.stop_reason

    def finish(self) -> Response:
        calls: list[ToolCall] = []
        for tid in self._tool_order:
            slot = self._tool_buf[tid]
            try:
                args = json.loads(slot["args_str"]) if slot["args_str"] else {}
            except json.JSONDecodeError:
                args = {}            # 容错：增量拼接异常 → 空 args（上层可修复）
            calls.append(ToolCall(id=tid, name=slot["name"], args=args if isinstance(args, dict) else {}))
        return Response(text="".join(self._text_parts), tool_calls=calls,
                        usage=self._usage, stop_reason=self._stop)


def assemble(events: Iterator[ProviderStreamEvent]) -> Response:
    a = StreamAssembler()
    for ev in events:
        a.feed(ev)
    return a.finish()
```

厂商流式事件 → `ProviderStreamEvent` 归一化（各 Provider 的 `stream()` 实现，字段以官方文档为准）：

| 供应商 | 厂商事件 | → `ProviderStreamEvent` |
|---|---|---|
| Anthropic | `content_block_delta`(`text_delta`) | `TEXT_DELTA` |
| | `content_block_delta`(`input_json_delta`) → `content_block_stop` | `TOOL_CALL_DELTA` → `TOOL_CALL_DONE` |
| | `message_delta`(usage) / `message_stop` | `USAGE` → `DONE` |
| OpenAI 兼容 | `choices[].delta.content` | `TEXT_DELTA` |
| | `choices[].delta.tool_calls[]`（增量 `function.arguments`）→ 结束 | `TOOL_CALL_DELTA` → `TOOL_CALL_DONE` |
| | 末块（`stream_options.include_usage`）`usage` | `USAGE` → `DONE` |
| ollama 原生 | 逐 chunk `message.content` | `TEXT_DELTA` |
| | 末 chunk `done=true` + `prompt_eval_count`/`eval_count` | `USAGE` → `DONE` |

流式收尾（`DONE`/`USAGE`）触发 `BudgetLedger.charge(usage)`（同非流式，Gateway 内）。

---

## 14.8 `structured.py`：结构化输出归一化（三策略，统一入口）

承接设计 §14.4。统一入口 `generate_structured(schema_model)`：各供应商走各自最强约束路径，统一用 Pydantic 校验 + instructor 式修复重试。**这是 §07 抽取（`ExtractSkill` 产 `BibleChangeProposal`）/Check 初筛/PromotionPolicy 输入的统一产出口**。

三策略由 Gateway 据能力矩阵选路（§14.9 能力降级），但**统一在 `generate_structured` 收口校验修复**：

| 供应商 | 首选结构化路径（`response_schema` 翻译） | 退路 |
|---|---|---|
| Anthropic（`structured_output=true`） | `tool_choice` 强制单一 strict 工具（模型只能产该工具入参）；或 `output_config.format=json_schema`（字段以官方文档为准） | 工具强制 |
| OpenAI 兼容（`structured_output=true`） | `response_format={type:"json_schema", json_schema:{name, schema, strict:true}}`（vLLM 经 guided decoding 等价） | `response_format={type:"json_object"}` + Pydantic |
| 本地 ollama（`structured_output=true`） | `format=<JSON Schema 对象>`（`/api/chat` 支持传 schema）；llama.cpp grammar/JSON-mode | JSON-mode + Pydantic 校验修复 |
| `structured_output=false`（任意） | JSON-mode + Pydantic 校验修复（instructor 式）；无 JSON-mode 则纯提示 + 校验 | 同左 |

```python
# novelforge/llm/structured.py
from __future__ import annotations
from typing import TYPE_CHECKING
from pydantic import BaseModel, ValidationError
from novelforge.llm.types import ChatMessage, Role, CacheHint
from novelforge.llm.tiers import ModelTier
from novelforge.llm.errors import StructuredOutputError

if TYPE_CHECKING:
    from novelforge.llm.gateway import LLMGateway


def generate_structured(
    gw: "LLMGateway", *, tier: ModelTier, schema_model: type[BaseModel],
    messages: list[ChatMessage], system: str | None = None,
    cache_hint: CacheHint | None = None, max_repair: int = 2,
) -> BaseModel:
    """统一结构化输出。原生 structured_output=true 多为一次过；修复重试是本地/弱网关安全网。
    schema 是稳定前缀的一部分（不随章节变），可进缓存断点（§14.6）。"""
    schema = schema_model.model_json_schema()
    last_err: Exception | None = None
    convo = list(messages)
    for attempt in range(max_repair + 1):
        resp = gw.generate(tier=tier, messages=convo, system=system,
                           response_schema=schema, cache_hint=cache_hint)
        text = _extract_payload(resp)
        try:
            return schema_model.model_validate_json(text)
        except ValidationError as e:                    # 自愈：带错误回灌重试（instructor 式）
            last_err = e
            convo = convo + [
                ChatMessage(role=Role.ASSISTANT, content=text),
                ChatMessage(role=Role.USER,
                            content=f"上次输出不满足 schema，错误：{e}\n请只输出修正后的合法 JSON。"),
            ]
    raise StructuredOutputError(f"structured output failed after {max_repair} repairs: {last_err}")


def _extract_payload(resp) -> str:
    """取结构化载荷：工具强制路径 → tool_calls[0].args 序列化；否则 resp.text。"""
    if resp.tool_calls:
        import json
        return json.dumps(resp.tool_calls[0].args, ensure_ascii=False)
    return resp.text
```

要点：①修复重试 token **计入会话预算**（每次 `gw.generate` 内已 `charge`），受 §07.6 断路器约束，防「修复风暴」烧预算（HP10）；②`LLMGateway.generate_structured`（接缝篇 §2.2 签名）内部即转调本函数（`self` 作 `gw`）；③产出 `BibleChangeProposal` 等结构化 diff 入 §08.7 抽取链 → §03 治理，LLM 绝不直写 canon（HP2）。

---

## 14.9 `gateway.py`：LLMGateway（核心装配）

承接设计 §14.5/§14.10。`LLMGateway` 对上是 Skill/ToolLoop 唯一 LLM 句柄，对下编排 Provider。职责：①档→model 映射；②能力降级查表；③`cache_hint` 装配；④记账 `charge`；⑤退避重试；⑥回退链 + `degraded` 标记。

### 14.9.1 档→model 映射

```python
# novelforge/llm/gateway.py（节选）
from novelforge.llm.config import ProvidersConfig, ProviderConfig
from novelforge.llm.tiers import ModelTier, normalize_tier


def resolve_model(cfg: ProvidersConfig, provider_id: str, tier: ModelTier) -> str:
    """档→该供应商 model ID（别名先归一到 fast/mid/strong）。"""
    pc = cfg.providers[provider_id]
    return pc.models[normalize_tier(tier).value]
```

### 14.9.2 能力降级查表（确定性，非模型自适应）

降级是**确定性查表**，每次降级写一条审计 `degraded` 标记（HP9）。返回一个「执行计划」`DegradePlan`，承载改写后的请求与降级原因。

```python
from dataclasses import dataclass, field
from novelforge.llm.types import Tool, CapabilitySet, CacheHint
from novelforge.llm.errors import CapabilityUnsupported
from novelforge.llm.tool_fallback import render_tool_protocol


@dataclass
class DegradePlan:
    req: dict                                 # 透传给 provider.generate 的 kwargs
    degraded: list[str] = field(default_factory=list)   # 降级原因码（写审计）
    prompt_tool_protocol: bool = False        # True=用提示式协议解析 ToolCall（§14.6）


def degrade_plan(caps: CapabilitySet, *, system: str | None, messages, tools,
                 response_schema, cache_hint, max_tokens, model) -> DegradePlan:
    req = dict(messages=messages, model=model, system=system, tools=tools,
               response_schema=response_schema, cache_hint=cache_hint, max_tokens=max_tokens)
    degraded: list[str] = []
    prompt_protocol = False

    # 1) tool_use 缺失 → 提示式 JSON 协议（把工具渲染进 system，清空原生 tools）
    if tools and not caps.tool_use:
        proto = render_tool_protocol(tools)
        req["system"] = (system + "\n\n" + proto) if system else proto
        req["tools"] = None
        prompt_protocol = True
        degraded.append("tool_use->prompt_protocol")

    # 2) structured_output 缺失 → 不传原生 response_schema（交 §14.8 instructor 修复重试）
    if response_schema and not caps.structured_output:
        req["response_schema"] = None
        degraded.append("structured_output->repair_retry")

    # 3) prompt_caching：auto/none 下 Provider 自行处理；此处仅保稳定前缀纪律（cache_hint 透传）
    #    （render_prompt 的污染断言在 Provider 翻译 cache_hint 时执行，§14.5）

    # 4) vision 缺失但 messages 含 image → 抛 CapabilityUnsupported（正文链路不依赖 vision，安全）
    if not caps.vision and _has_image(messages):
        raise CapabilityUnsupported(f"model {model} lacks vision but image content present")

    # 5) parallel_tool_calls 缺失 → 无需改请求（ToolLoop 本就按步串行消费），不记降级
    return DegradePlan(req=req, degraded=degraded, prompt_tool_protocol=prompt_protocol)
```

具体代码路径（设计要求逐一对应）：
- `if provider.caps.tool_use else 提示式协议`：→ `degrade_plan` 分支 1：渲染 `render_tool_protocol(tools)` 进 system、清空 `tools`、置 `prompt_tool_protocol=True`；调用后 Gateway 用 `parse_tool_protocol(resp.text)` 解析 `ToolCall`（见 §14.9.5）。
- `if structured_output else instructor 式`：→ `degrade_plan` 分支 2：清空原生 `response_schema`；`generate_structured`（§14.8）的 Pydantic 校验-修复重试承接。
- `if prompt_caching else no-op 但保持前缀稳定`：→ 分支 3 不改请求，`cache_hint` 透传；Provider 按 `caps.prompt_caching`（`explicit`/`auto`/`none`）决定打断点/字节稳定/no-op，三者都先过 `render_prompt` 的 HP10 污染断言（§14.5）。

### 14.9.3 错误码归一化（各厂商 → 统一类）

```python
def classify_error(provider_id: str, e: Exception) -> str:
    """各厂商错误 → 统一归一类字符串（= ProviderError.error_class）。
    决定重试 / 回退 / 直接失败（§14.10.1 处置表）。"""
    if isinstance(e, RateLimitError):
        return "rate_limited"
    if isinstance(e, ServerError):
        return "server_error"
    if isinstance(e, ProviderError) and e.error_class:
        return e.error_class                  # Provider 已归一（首选）
    # 兜底：按 status_code
    code = getattr(e, "status_code", None)
    if code == 429:
        return "rate_limited"
    if code in (401, 403):
        return "auth_error"
    if code == 400:
        return "bad_request"
    if code and 500 <= code < 600:
        return "server_error"
    return "unavailable"                      # 连接失败/进程未起 → 触发回退
```

统一类与处置（设计 §14.10.1 表）：

| 统一类 | Anthropic | OpenAI 兼容 | ollama | 处置 |
|---|---|---|---|---|
| `rate_limited` | 429 | 429 | 429/模型忙 | 同供应商指数退避重试 |
| `server_error` | 5xx / `overloaded_error` | 5xx | 5xx | 退避重试，超次→回退 |
| `timeout` | 读超时 | 读超时 | 读超时 | 退避重试，超次→回退 |
| `auth_error` | 401/403 | 401/403 | —— | 不重试、不回退（配置错，直接抛） |
| `bad_request` | 400（schema/参数） | 400 | 400 | 不重试（除结构化修复 §14.8）；记 `degraded` |
| `unavailable` | 连接失败 | 连接失败 | 进程未起 | 直接触发回退链 |

### 14.9.4 退避（指数 + jitter）

```python
import random, time


def sleep_backoff(retry, attempt: int) -> int:
    """指数退避 + 可选 jitter。返回实际睡眠毫秒（供测试断言/计会话预算耗时）。
    base_ms * 2^attempt，封顶 max_ms；jitter=±50% 抖动。"""
    delay = min(retry.base_ms * (2 ** attempt), retry.max_ms)
    if retry.jitter:
        delay = int(delay * (0.5 + random.random()))     # [0.5,1.5)×
        delay = min(delay, retry.max_ms)
    time.sleep(delay / 1000.0)
    return delay
```

> 测试中通过 monkeypatch `time.sleep` 为 no-op、注入固定 `random.random`，断言重试次数与退避序列，不实际睡眠。

### 14.9.5 generate：完整装配（降级→退避→回退→记账）

```python
# novelforge/llm/gateway.py（核心）
import logging
from typing import Iterator
from pydantic import BaseModel
from novelforge.llm.types import (
    ChatMessage, Tool, Response, CacheHint, ProviderStreamEvent, Usage,
)
from novelforge.llm.errors import ProviderError, AllProvidersFailed
from novelforge.llm.pricing import pricing_for
from novelforge.llm.tool_fallback import parse_tool_protocol
from novelforge.llm.providers import build_providers

log = logging.getLogger("novelforge.llm")


class LLMGateway:
    def __init__(self, cfg: ProvidersConfig, budget: "BudgetLedger") -> None:
        self.cfg = cfg
        self.budget = budget
        self.providers = build_providers(cfg)        # {id: LLMProvider}；缺 SDK 的 type 跳过
        self._last_usage = Usage()
        self._prefix_seen: set[int] = set()          # 缓存命中告警去重（按 stable 前缀 hash）

    # ---- §12 ToolLoop 唯一入口 ----
    def generate(self, *, tier: ModelTier, messages: list[ChatMessage],
                 system: str | None = None, tools: list[Tool] | None = None,
                 response_schema: dict | None = None, cache_hint: CacheHint | None = None,
                 max_tokens: int = 4096) -> Response:
        chain = self.cfg.chain()                      # [default, *fallback] 去重保序
        last: Exception | None = None
        for pid in chain:
            prov = self.providers.get(pid)
            if prov is None:                          # 该 type 因缺 SDK 未构建 → 跳到下一供应商
                last = AllProvidersFailed([pid], None)
                continue
            model = resolve_model(self.cfg, pid, tier)
            caps = prov.capabilities(model)
            plan = degrade_plan(caps, system=system, messages=messages, tools=tools,
                                response_schema=response_schema, cache_hint=cache_hint,
                                max_tokens=max_tokens, model=model)
            retry = self.cfg.providers[pid].retry
            for attempt in range(retry.max_attempts):
                try:
                    resp = prov.generate(**plan.req)
                    resp = self._post_process(resp, plan, caps, cache_hint, tier, pid)
                    return resp
                except ProviderError as e:
                    last = e
                    cls = classify_error(pid, e)
                    if cls in ("auth_error", "bad_request"):
                        raise                          # 不重试不回退（配置/请求错）
                    if cls in ("rate_limited", "server_error", "timeout") and attempt + 1 < retry.max_attempts:
                        sleep_backoff(retry, attempt)  # 指数退避+jitter；耗时计会话预算（§07.6）
                        continue
                    break                              # 退避用尽 or unavailable → 换下一供应商
            # 此供应商失败，回退链继续
        raise AllProvidersFailed(chain, last)

    def _post_process(self, resp: Response, plan: DegradePlan, caps, cache_hint,
                      tier: ModelTier, pid: str) -> Response:
        # 1) 提示式协议降级 → 从 text 解析 ToolCall（与原生形态一致）
        if plan.prompt_tool_protocol and not resp.tool_calls:
            calls, answer = parse_tool_protocol(resp.text)
            resp = resp.model_copy(update={"tool_calls": calls,
                                           "text": answer if calls == [] else ""})
        # 2) 记账：charge 进 BudgetLedger（usage 已含 provider/model）
        pricing = pricing_for(self.cfg.providers[pid], tier)
        self._last_usage = resp.usage
        self.budget.charge(resp.usage)                # LLMUsage=Usage；charge 内调 billable()/usd(pricing)
        # 3) 缓存命中告警（silent invalidator）
        self._cache_warn(resp.usage, caps, cache_hint)
        # 4) degraded 审计标记：写入 resp.raw 旁路（ToolLoop 据此落 tool_call_log.note）
        if plan.degraded:
            resp = resp.model_copy(update={"raw": {**(resp.raw or {}), "_degraded": plan.degraded}})
        return resp

    def _cache_warn(self, usage: Usage, caps, cache_hint: CacheHint | None) -> None:
        if not cache_hint or caps.prompt_caching == "none":
            return                                    # 本地 none：cache_read=0 正常，抑制告警
        key = hash(tuple(cache_hint.stable_blocks))
        first = key not in self._prefix_seen
        self._prefix_seen.add(key)
        if not first and usage.cache_read == 0:
            log.warning("prefix cache MISS — 检查前缀是否被章节号/uuid/检索结果污染")

    # ---- §07.6.2 薄包装：保留兼容旧 Skill ----
    def call(self, *, tier: ModelTier, system_stable: str, dynamic: str,
             cache_prefix: bool = True, tools: list[Tool] | None = None) -> Response:
        hint = CacheHint(stable_blocks=[system_stable]) if cache_prefix else None
        return self.generate(tier=tier, system=system_stable,
                             messages=[ChatMessage(role=Role.USER, content=dynamic)],
                             tools=tools, cache_hint=hint)

    # ---- 流式：经 Orchestrator 转译为 §13 业务级 SSE ----
    def stream(self, *, tier: ModelTier, messages: list[ChatMessage],
               system: str | None = None, tools: list[Tool] | None = None,
               cache_hint: CacheHint | None = None, max_tokens: int = 4096
               ) -> Iterator[ProviderStreamEvent]:
        pid = self.cfg.default                        # 流式默认不跨供应商回退（中途切换体验差）
        prov = self.providers[pid]
        model = resolve_model(self.cfg, pid, tier)
        caps = prov.capabilities(model)
        plan = degrade_plan(caps, system=system, messages=messages, tools=tools,
                            response_schema=None, cache_hint=cache_hint,
                            max_tokens=max_tokens, model=model)
        plan.req.pop("response_schema", None); plan.req.pop("stream", None)
        last_usage = Usage()
        for ev in prov.stream(**plan.req):
            if ev.usage is not None:
                last_usage = ev.usage
            yield ev
        self._last_usage = last_usage
        self.budget.charge(last_usage)                # 流式收尾记账（同非流式）

    # ---- 结构化输出统一入口（§14.8 转调） ----
    def generate_structured(self, *, tier: ModelTier, schema_model: type[BaseModel],
                            messages: list[ChatMessage], system: str | None = None,
                            cache_hint: CacheHint | None = None, max_repair: int = 2) -> BaseModel:
        from novelforge.llm.structured import generate_structured as _gs
        return _gs(self, tier=tier, schema_model=schema_model, messages=messages,
                   system=system, cache_hint=cache_hint, max_repair=max_repair)

    def last_usage(self) -> Usage:
        return self._last_usage
```

### 14.9.6 回退链纪律（设计 §14.10）

1. 回退只在「供应商整体不可用 / 退避用尽」时触发；单次 `bad_request` 不回退（避免把 schema 错误扩散到所有供应商）。
2. 回退后实际 `provider`/`model` 写入 `Usage`（由 Provider 在归一化 usage 时填）与审计 `tool_call_log.provider`/`model`，使「这一章哪段用了哪个供应商」可追（HP9）。
3. 回退与 §07.6 断路器叠加——回退重试的 token/耗时仍 `charge` 进会话预算，超限照样熔断（HP10），不因回退绕过成本生死线。
4. 流式 `stream()` **不跨供应商回退**（中途切供应商会破坏 SSE 连续性），只在 `default` 供应商上做同供应商退避；失败直接上抛由 Orchestrator 决定 held。

---

## 14.10 各 Provider：请求装配 / 响应解析（精确到字段）

> 下列字段名/模型 ID/SSE 事件名均为工程结构示意，**以各厂商官方最新文档为准，可用 context7 核对**（`anthropic` / `openai` / `ollama`）。

### 14.10.1 `AnthropicProvider`（`providers/anthropic.py`）

```python
# novelforge/llm/providers/anthropic.py
from __future__ import annotations
from typing import Iterator
from novelforge.llm.types import (
    ChatMessage, Role, Tool, ToolCall, Response, Usage, CapabilitySet,
    CacheHint, ProviderStreamEvent, StreamEventType, StopReason,
)
from novelforge.llm.errors import ProviderError, RateLimitError, ServerError
from novelforge.llm.pricing import normalize_anthropic_usage

try:
    import anthropic                       # 可选 SDK
    _HAS = True
except ImportError:
    anthropic = None; _HAS = False


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, base_url: str, api_key: str | None, caps: CapabilitySet):
        if not _HAS:
            raise ProviderError("anthropic SDK not installed", error_class="unavailable",
                                provider=self.name)
        self._client = anthropic.Anthropic(api_key=api_key, base_url=base_url or None)
        self._caps = caps

    def capabilities(self, model: str) -> CapabilitySet:
        return self._caps

    # ---- 请求装配 ----
    def _to_tools(self, tools: list[Tool] | None) -> list[dict] | None:
        if not tools:
            return None
        return [{
            "name": t.name, "description": t.description,
            "input_schema": t.json_schema,                 # ← json_schema 落 input_schema
            **({"strict": True} if t.strict else {}),
        } for t in tools]

    def _to_messages(self, messages: list[ChatMessage]) -> list[dict]:
        out = []
        for m in messages:
            if m.role == Role.TOOL:                         # tool result → tool_result 块
                out.append({"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": m.tool_call_id,
                    "content": m.content if isinstance(m.content, str) else m.content}]})
            elif m.role == Role.ASSISTANT and m.tool_calls:  # 模型上一轮 tool_use 回放
                out.append({"role": "assistant", "content": [
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args}
                    for tc in m.tool_calls]})
            else:
                out.append({"role": m.role.value, "content": m.content})
        return out

    def _apply_cache(self, system: str | None, tools: list[dict] | None,
                     hint: CacheHint | None):
        """explicit 缓存：在 system / tools 末块打 cache_control 断点（HP10 稳定前缀）。"""
        if not hint:
            return system, tools
        sys_blocks = ([{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral", "ttl": hint.ttl}}]
                      if system else None)
        if tools:
            tools = list(tools)
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral", "ttl": hint.ttl}}
        return sys_blocks, tools

    def generate(self, *, messages, model, system=None, tools=None,
                 response_schema=None, cache_hint=None, stream=False, max_tokens=4096) -> Response:
        if stream:
            from novelforge.llm.stream import assemble
            return assemble(self.stream(messages=messages, model=model, system=system,
                                        tools=tools, response_schema=response_schema,
                                        cache_hint=cache_hint, max_tokens=max_tokens))
        api_tools = self._to_tools(tools)
        tool_choice = None
        if response_schema:                               # 结构化 → 工具强制（产单一 strict 工具入参）
            api_tools = (api_tools or []) + [{"name": "emit_structured",
                "description": "emit the structured result", "input_schema": response_schema,
                "strict": True}]
            tool_choice = {"type": "tool", "name": "emit_structured"}
        sys_blocks, api_tools = self._apply_cache(system, api_tools, cache_hint)
        try:
            r = self._client.messages.create(
                model=model, max_tokens=max_tokens,
                system=sys_blocks, messages=self._to_messages(messages),
                tools=api_tools, tool_choice=tool_choice)
        except Exception as e:                            # 归一化厂商异常
            raise self._map_error(e)
        return self._parse(r, model)

    # ---- 响应解析（精确到字段）----
    def _parse(self, r, model: str) -> Response:
        text_parts, calls = [], []
        for block in getattr(r, "content", []):           # content[]: text / tool_use 块
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", ""))
            elif btype == "tool_use":                     # id / name / input
                calls.append(ToolCall(id=block.id, name=block.name,
                                      args=dict(getattr(block, "input", {}) or {})))
        usage = normalize_anthropic_usage(r.usage, provider=self.name, model=model)
        stop = {"end_turn": StopReason.STOP, "tool_use": StopReason.TOOL_USE,
                "max_tokens": StopReason.MAX_TOKENS}.get(getattr(r, "stop_reason", ""),
                                                         StopReason.STOP)
        return Response(text="".join(text_parts), tool_calls=calls, usage=usage,
                        stop_reason=stop, raw=None)        # raw 不入库（§14.9 红线）

    def stream(self, *, messages, model, system=None, tools=None,
               response_schema=None, cache_hint=None, max_tokens=4096
               ) -> Iterator[ProviderStreamEvent]:
        api_tools = self._to_tools(tools)
        sys_blocks, api_tools = self._apply_cache(system, api_tools, cache_hint)
        try:
            with self._client.messages.stream(model=model, max_tokens=max_tokens,
                    system=sys_blocks, messages=self._to_messages(messages),
                    tools=api_tools) as s:
                cur_tool: dict | None = None
                for ev in s:                              # SSE: content_block_delta / _start / _stop
                    et = getattr(ev, "type", "")
                    if et == "content_block_start" and getattr(ev.content_block, "type", "") == "tool_use":
                        cur_tool = {"id": ev.content_block.id, "name": ev.content_block.name}
                    elif et == "content_block_delta":
                        d = ev.delta
                        if getattr(d, "type", "") == "text_delta":
                            yield ProviderStreamEvent(type=StreamEventType.TEXT_DELTA, text=d.text)
                        elif getattr(d, "type", "") == "input_json_delta":
                            yield ProviderStreamEvent(type=StreamEventType.TOOL_CALL_DELTA,
                                tool_call=ToolCall(id=cur_tool["id"], name=cur_tool["name"], args={}),
                                partial_args=d.partial_json)
                    elif et == "content_block_stop" and cur_tool:
                        yield ProviderStreamEvent(type=StreamEventType.TOOL_CALL_DONE,
                            tool_call=ToolCall(id=cur_tool["id"], name=cur_tool["name"], args={}))
                        cur_tool = None
                final = s.get_final_message()
                usage = normalize_anthropic_usage(final.usage, provider=self.name, model=model)
                yield ProviderStreamEvent(type=StreamEventType.DONE, usage=usage,
                                          stop_reason=StopReason.STOP)
        except Exception as e:
            raise self._map_error(e)

    def _map_error(self, e: Exception) -> ProviderError:
        sc = getattr(e, "status_code", None)
        name = type(e).__name__
        if "RateLimit" in name or sc == 429:
            return RateLimitError(str(e), error_class="rate_limited", provider=self.name, status_code=429)
        if "Overloaded" in name or (sc and 500 <= sc < 600):
            return ServerError(str(e), error_class="server_error", provider=self.name, status_code=sc)
        if sc in (401, 403):
            return ProviderError(str(e), error_class="auth_error", provider=self.name, status_code=sc)
        if sc == 400:
            return ProviderError(str(e), error_class="bad_request", provider=self.name, status_code=400)
        if "APIConnection" in name or "Timeout" in name:
            return ProviderError(str(e), error_class="timeout", provider=self.name)
        return ProviderError(str(e), error_class="unavailable", provider=self.name, status_code=sc)
```

Anthropic 关键字段：请求 `tools[].input_schema`（← `Tool.json_schema`）/ `tool_choice={"type":"tool","name":...}`（结构化强制）/ `cache_control={"type":"ephemeral","ttl"}`（system/tools 末块断点）；响应 `content[]` 的 `tool_use` 块（`id`/`name`/`input`）/ `usage.cache_read_input_tokens`/`cache_creation_input_tokens`；SSE `content_block_delta`（`text_delta`/`input_json_delta`）→ `content_block_stop`。

### 14.10.2 `OpenAICompatProvider`（`providers/openai_compat.py`）

```python
# 核心差异（节选）；结构与 AnthropicProvider 平行
try:
    import openai
    _HAS = True
except ImportError:
    openai = None; _HAS = False


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(self, *, base_url: str, api_key: str | None, caps: CapabilitySet):
        if not _HAS:
            raise ProviderError("openai SDK not installed", error_class="unavailable",
                                provider=self.name)
        self._client = openai.OpenAI(api_key=api_key or "EMPTY", base_url=base_url or None)
        self._caps = caps

    def _to_tools(self, tools):                            # → OpenAI function tools
        if not tools:
            return None
        return [{"type": "function", "function": {
            "name": t.name, "description": t.description,
            "parameters": t.json_schema,                   # ← json_schema 落 function.parameters
            **({"strict": True} if t.strict else {})}} for t in tools]

    def _to_messages(self, messages, system):
        out = ([{"role": "system", "content": system}] if system else [])
        for m in messages:
            if m.role == Role.TOOL:                         # tool result → role:"tool"
                out.append({"role": "tool", "tool_call_id": m.tool_call_id,
                            "content": m.content, "name": m.name})
            elif m.role == Role.ASSISTANT and m.tool_calls:
                out.append({"role": "assistant", "content": m.content or None,
                    "tool_calls": [{"id": tc.id, "type": "function",
                        "function": {"name": tc.name,
                                     "arguments": __import__("json").dumps(tc.args)}}
                                   for tc in m.tool_calls]})
            else:
                out.append({"role": m.role.value, "content": m.content})
        return out

    def generate(self, *, messages, model, system=None, tools=None,
                 response_schema=None, cache_hint=None, stream=False, max_tokens=4096) -> Response:
        if stream:
            from novelforge.llm.stream import assemble
            return assemble(self.stream(messages=messages, model=model, system=system,
                                        tools=tools, cache_hint=cache_hint, max_tokens=max_tokens))
        kw = dict(model=model, max_tokens=max_tokens,
                  messages=self._to_messages(messages, system), tools=self._to_tools(tools))
        if response_schema:                                # response_format=json_schema
            kw["response_format"] = {"type": "json_schema", "json_schema":
                {"name": "structured", "schema": response_schema, "strict": True}}
        try:
            r = self._client.chat.completions.create(**kw)  # cache_hint=auto → 无显式断点，前缀字节稳定
        except Exception as e:
            raise self._map_error(e)
        return self._parse(r, model)

    def _parse(self, r, model) -> Response:
        choice = r.choices[0]; msg = choice.message
        calls = []
        for tc in (getattr(msg, "tool_calls", None) or []):
            import json
            try:
                args = json.loads(tc.function.arguments)    # arguments 是 JSON 串 → dict
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=tc.id, name=tc.function.name,
                                  args=args if isinstance(args, dict) else {}))
        from novelforge.llm.pricing import normalize_openai_usage
        usage = normalize_openai_usage(r.usage, provider=self.name, model=model)
        stop = {"stop": StopReason.STOP, "tool_calls": StopReason.TOOL_USE,
                "length": StopReason.MAX_TOKENS}.get(getattr(choice, "finish_reason", ""), StopReason.STOP)
        return Response(text=(msg.content or ""), tool_calls=calls, usage=usage, stop_reason=stop)

    def stream(self, *, messages, model, system=None, tools=None,
               cache_hint=None, max_tokens=4096) -> Iterator[ProviderStreamEvent]:
        try:
            s = self._client.chat.completions.create(
                model=model, max_tokens=max_tokens,
                messages=self._to_messages(messages, system), tools=self._to_tools(tools),
                stream=True, stream_options={"include_usage": True})   # ← include_usage 拿末块 usage
            for chunk in s:
                if chunk.usage:                             # 末块 usage（include_usage）
                    from novelforge.llm.pricing import normalize_openai_usage
                    yield ProviderStreamEvent(type=StreamEventType.USAGE,
                        usage=normalize_openai_usage(chunk.usage, provider=self.name, model=model))
                    continue
                if not chunk.choices:
                    continue
                d = chunk.choices[0].delta
                if getattr(d, "content", None):
                    yield ProviderStreamEvent(type=StreamEventType.TEXT_DELTA, text=d.content)
                for tc in (getattr(d, "tool_calls", None) or []):       # 增量 tool_calls
                    yield ProviderStreamEvent(type=StreamEventType.TOOL_CALL_DELTA,
                        tool_call=ToolCall(id=tc.id or "", name=(tc.function.name or ""), args={}),
                        partial_args=(tc.function.arguments or ""))
            yield ProviderStreamEvent(type=StreamEventType.DONE, stop_reason=StopReason.STOP)
        except Exception as e:
            raise self._map_error(e)
    # _map_error 同 AnthropicProvider 形态（按 openai.RateLimitError/APIStatusError 映射）
```

OpenAI 关键字段：请求 `tools[].function.parameters`（← `Tool.json_schema`）/ `response_format={type:"json_schema",...}` / `stream_options={"include_usage":true}`；响应 `choices[0].message.tool_calls[]`（`id`/`function.name`/`function.arguments` 为 JSON 串，需 `json.loads`）/ `usage.prompt_tokens`/`completion_tokens`/`prompt_tokens_details.cached_tokens`。`auto` 缓存无显式断点——靠前缀字节稳定（顺序固定、dynamic 后置）。

### 14.10.3 `LocalProvider`（`providers/local.py`，ollama/llama.cpp）

```python
# novelforge/llm/providers/local.py
try:
    import httpx                                          # 可选；缺则用 urllib 兜底
    _HAS_HTTPX = True
except ImportError:
    httpx = None; _HAS_HTTPX = False


class LocalProvider:
    name = "local_ollama"

    def __init__(self, *, base_url: str, api_key=None, caps: CapabilitySet,
                 read_s: int = 600):
        self._base = base_url.rstrip("/")
        self._caps = caps
        self._read_s = read_s

    def capabilities(self, model: str) -> CapabilitySet:
        return self._caps

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self._base}{path}"
        try:
            if _HAS_HTTPX:
                r = httpx.post(url, json=payload, timeout=self._read_s)
                if r.status_code != 200:
                    raise self._map_status(r.status_code, r.text)
                return r.json()
            import json as _j, urllib.request, urllib.error
            req = urllib.request.Request(url, data=_j.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self._read_s) as resp:
                return _j.loads(resp.read())
        except ProviderError:
            raise
        except Exception as e:                            # 连接失败/进程未起 → unavailable（触发回退）
            raise ProviderError(str(e), error_class="unavailable", provider=self.name)

    def generate(self, *, messages, model, system=None, tools=None,
                 response_schema=None, cache_hint=None, stream=False, max_tokens=4096) -> Response:
        payload = {"model": model, "stream": False,
                   "messages": self._to_messages(messages, system),
                   "options": {"num_predict": max_tokens}}
        if tools and self._caps.tool_use:                 # ollama /api/chat tools（新模型）
            payload["tools"] = self._to_tools(tools)
        if response_schema and self._caps.structured_output:
            payload["format"] = response_schema           # ollama format=<JSON Schema 对象>
        data = self._post("/api/chat", payload)
        return self._parse(data, model)

    def _parse(self, data: dict, model) -> Response:
        msg = data.get("message", {})
        calls = []
        for tc in (msg.get("tool_calls") or []):          # function.name / function.arguments（已是对象）
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            calls.append(ToolCall(id=new_id("tc"), name=fn.get("name", ""),
                                  args=args if isinstance(args, dict) else {}))
        from novelforge.llm.pricing import normalize_ollama_usage
        usage = normalize_ollama_usage(data, provider=self.name, model=model)
        stop = StopReason.TOOL_USE if calls else StopReason.STOP
        return Response(text=msg.get("content", ""), tool_calls=calls, usage=usage, stop_reason=stop)

    def stream(self, *, messages, model, system=None, tools=None,
               cache_hint=None, max_tokens=4096):
        # ollama stream=True：逐 chunk message.content；末 chunk done=true + eval_count
        import json as _j
        payload = {"model": model, "stream": True,
                   "messages": self._to_messages(messages, system),
                   "options": {"num_predict": max_tokens}}
        # （httpx.stream / urllib 逐行读 NDJSON；每行 json）
        for line in self._iter_ndjson("/api/chat", payload):
            obj = _j.loads(line)
            if obj.get("done"):
                from novelforge.llm.pricing import normalize_ollama_usage
                yield ProviderStreamEvent(type=StreamEventType.DONE,
                    usage=normalize_ollama_usage(obj, provider=self.name, model=model),
                    stop_reason=StopReason.STOP)
            else:
                content = obj.get("message", {}).get("content", "")
                if content:
                    yield ProviderStreamEvent(type=StreamEventType.TEXT_DELTA, text=content)

    def _map_status(self, code: int, body: str) -> ProviderError:
        if code == 429:
            return RateLimitError(body, error_class="rate_limited", provider=self.name, status_code=429)
        if 500 <= code < 600:
            return ServerError(body, error_class="server_error", provider=self.name, status_code=code)
        if code == 400:
            return ProviderError(body, error_class="bad_request", provider=self.name, status_code=400)
        return ProviderError(body, error_class="unavailable", provider=self.name, status_code=code)
    # _to_tools / _to_messages / _iter_ndjson 略（同形态）
```

ollama 关键字段：请求 `/api/chat` 的 `tools`（新模型支持，与 OpenAI 同形）/ `format=<JSON Schema 对象>`（结构化）；响应 `message.content` / `message.tool_calls[]`（`function.name`/`function.arguments` **已是对象**，无需 `json.loads`）/ `prompt_eval_count`/`eval_count`；stream NDJSON 末块 `done=true`。`caps.tool_use=False`（旧模型）时 Gateway 已在 `degrade_plan` 把 tools 渲染进 system，`LocalProvider` 不收到 `tools`，纯文本生成 → Gateway 用 `parse_tool_protocol` 还原 `ToolCall`（提示式协议 §14.6）。`prompt_caching=none` → 无缓存字段，`cache_read=0`，告警抑制。

### 14.10.4 `build_providers` 工厂（缺 SDK 跳过/降级）

```python
# novelforge/llm/providers/__init__.py
from novelforge.llm.config import ProvidersConfig
from novelforge.llm.errors import ProviderError


def build_providers(cfg: ProvidersConfig) -> dict[str, "LLMProvider"]:
    """按 type 构建 Provider。缺 SDK/构造失败的供应商 → 跳过（不进 dict），
    Gateway 调用时 self.providers.get(pid) 为 None → 顺到下一供应商（回退）。"""
    from novelforge.llm.providers.fake import FakeProvider
    out: dict[str, "LLMProvider"] = {}
    for pid, pc in cfg.providers.items():
        try:
            key = pc.resolve_key() if pc.api_key_env else None
            if pc.type == "anthropic":
                from novelforge.llm.providers.anthropic import AnthropicProvider
                out[pid] = AnthropicProvider(base_url=pc.base_url, api_key=key, caps=pc.capabilities)
            elif pc.type == "openai_compat":
                from novelforge.llm.providers.openai_compat import OpenAICompatProvider
                out[pid] = OpenAICompatProvider(base_url=pc.base_url, api_key=key, caps=pc.capabilities)
            elif pc.type == "local_ollama":
                from novelforge.llm.providers.local import LocalProvider
                out[pid] = LocalProvider(base_url=pc.base_url, caps=pc.capabilities,
                                         read_s=pc.timeout.read_s)
            elif pc.type == "fake":
                out[pid] = FakeProvider(caps=pc.capabilities)   # 测试用（脚本由测试注入）
        except ProviderError:
            continue            # 缺 SDK / key 未设 → 跳过该供应商（回退链覆盖）
    return out
```

---

## 14.11 边界情况清单（逐条）

| # | 边界 | 触发点 | 处置（实现路径） |
|---|---|---|---|
| B1 | Provider 缺 `tool_use` | `degrade_plan` 分支1 | 渲染 `render_tool_protocol` 进 system、清空原生 tools；`_post_process` 用 `parse_tool_protocol` 还原 `ToolCall`；标 `degraded=["tool_use->prompt_protocol"]` |
| B2 | 结构化输出畸形，修复 N 次仍失败 | `generate_structured` 循环耗尽 | 抛 `StructuredOutputError`；调用方（ExtractSkill/Check）按降级处理，**不丢章**（正文照常落 L0） |
| B3 | 流式中途 `RateLimit`/异常 | `stream()` 内 Provider 异常 | 不跨供应商回退（破坏 SSE 连续性）；映射为 `ProviderError` 上抛，Orchestrator 据此 held 或回退到非流式重试 |
| B4 | 回退链耗尽 | `generate` 走完 `chain` 全失败 | 抛 `AllProvidersFailed(chain, last)`；Orchestrator 捕获 → 章节 held + 标记，不静默丢弃 |
| B5 | 缓存未命中告警 | `_cache_warn` | 重复稳定前缀 `cache_read==0` 且 `caps.prompt_caching!="none"` → `log.warning`；本地 `none` 抑制 |
| B6 | 本地模型返回非 JSON（结构化/工具） | `parse_tool_protocol`/`_extract_payload` 解析失败 | 工具：空 `tool_calls` → 触发上层修复/降级；结构化：`ValidationError` → instructor 修复重试 |
| B7 | `auth_error`（key 错/未设） | `classify_error`→`auth_error` | 立即抛，**不重试不回退**（配置错扩散到所有供应商无意义） |
| B8 | `bad_request`（schema/参数错） | `classify_error`→`bad_request` | 不重试不回退（除结构化修复）；记 `degraded`；单次不扩散到其它供应商 |
| B9 | Provider 缺 SDK | `build_providers` 构造抛 `ProviderError` | 跳过该供应商（不进 dict）；`generate` 中 `providers.get(pid) is None` → 顺到下一供应商 |
| B10 | `vision` 缺失但消息含 image | `degrade_plan` 分支4 | 抛 `CapabilityUnsupported`（正文链路不依赖 vision，安全） |
| B11 | 稳定前缀被章节号/uuid/时间戳污染 | `render_prompt`/`_apply_cache` | `assert not polluted`（AssertionError，开发期硬失败，HP10） |
| B12 | 退避用尽（同供应商重试达 `max_attempts`） | `generate` 内层 for 跑完 | `break` → 换下一供应商；全失败 → B4 |
| B13 | 流式 tool_call 增量 JSON 拼接不合法 | `StreamAssembler.finish` | `json.loads` 失败 → 空 `args`（容错，上层可修复） |
| B14 | ollama tool 无 `id`（厂商不返回） | `LocalProvider._parse` | 用 `new_id("tc")` 生成（接缝 `ToolCall.id` 必填） |
| B15 | 回退后 provider/model 漂移 | `Provider` 归一化 usage | `Usage.provider`/`model` 填实际承接供应商；审计 `tool_call_log.provider`/`model` 可追（HP9） |
| B16 | 修复重试烧预算 | `generate_structured` 每次 `gw.generate` 内 `charge` | 受 §07.6 `breaker.guard()` 约束；超限 `CircuitTripped`（由 ToolLoop/Orchestrator 捕获） |
| B17 | key 误入日志/raw | `Response.raw` / `_post_process` | `raw` 不落库；`degraded` 写 `raw["_degraded"]` 仅供 ToolLoop 读 note，落库前 ToolLoop 只取 note 字符串 |

---

## 14.12 时序图：generate（含 tool_use 一轮 + 重试）

```mermaid
sequenceDiagram
    autonumber
    participant TL as ToolLoop (§12)
    participant GW as LLMGateway (§14.9)
    participant DP as degrade_plan
    participant PR as AnthropicProvider (§14.10.1)
    participant API as Anthropic SDK
    participant BL as BudgetLedger (§07.6)

    TL->>GW: generate(tier=STRONG, system_stable, messages, tools, cache_hint)
    GW->>GW: chain = [default, *fallback]
    GW->>PR: capabilities(model)  →  CapabilitySet
    GW->>DP: degrade_plan(caps, req)
    DP-->>GW: DegradePlan(req, degraded=[])  (caps.tool_use=True → 无降级)

    rect rgb(245,238,225)
    Note over GW,API: attempt 0 — 429 退避重试
    GW->>PR: generate(**plan.req)
    PR->>API: messages.create(tools[].input_schema, cache_control 断点)
    API-->>PR: 429 RateLimitError
    PR-->>GW: raise RateLimitError(error_class="rate_limited")
    GW->>GW: classify→rate_limited; sleep_backoff(retry, 0)  (指数+jitter)
    end

    rect rgb(232,242,232)
    Note over GW,API: attempt 1 — 成功，返回 tool_use
    GW->>PR: generate(**plan.req)
    PR->>API: messages.create(...)
    API-->>PR: content[tool_use{id,name,input}], usage{cache_read_input_tokens>0}
    PR->>PR: _parse → Response{tool_calls=[ToolCall(args=input)], usage}
    PR-->>GW: Response
    end

    GW->>BL: charge(usage)   (billable()/usd(pricing))
    GW->>GW: _cache_warn(usage, caps, hint)  (cache_read>0 → 无告警)
    GW-->>TL: Response{text="", tool_calls=[ToolCall], usage, stop_reason=tool_use}

    Note over TL: ToolLoop 执行 ToolCall → ToolResult → 回灌 messages，进入下一轮
```

---

## 14.13 pytest 测试计划

> 全套零外网、零 SDK、零 key 可跑：`FakeProvider`（接缝篇 §7.1）+ monkeypatch `time.sleep`/`random`。真实 Provider 集成测试标 `@pytest.mark.integration`，默认 `-m "not integration"` 跳过。

### 14.13.1 FakeProvider 与 fixtures

```python
# tests/llm/conftest.py
import itertools, pytest
from novelforge.llm.types import Response, Usage, ToolCall, CapabilitySet
from novelforge.llm.providers.fake import FakeProvider

@pytest.fixture
def caps_full():
    return CapabilitySet(tool_use=True, structured_output=True, prompt_caching="explicit",
                         streaming=True, parallel_tool_calls=True, vision=True)

@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("novelforge.llm.gateway.time.sleep", lambda s: None)
    monkeypatch.setattr("novelforge.llm.gateway.random.random", lambda: 0.5)  # 确定性退避

@pytest.fixture
def gateway_factory(caps_full):
    """构造 default=fake (+可选 fallback=fake2) 的 LLMGateway，注入 budget spy。"""
    ...   # 用 ProvidersConfig(type="fake") + FakeProvider(script=..., errors=...) 装配
```

> `FakeProvider`（接缝篇 §7.1）：`__init__(*, script, caps, errors)`；`generate` 弹 `script[i]`（含 `tool_calls`→驱动循环）或先弹 `errors[i]`（测退避/回退）；`stream` yield 预置 `ProviderStreamEvent`。`build_providers` 对 `type=="fake"` 返回它（脚本由测试经 `gateway.providers[pid].script = [...]` 注入）。

### 14.13.2 测试用例表

#### A. config 加载与校验（`tests/llm/test_config.py`）

| test 函数 | 断言要点 |
|---|---|
| `test_config_requires_all_tiers` | `models` 缺 strong → `model_validator` AssertionError |
| `test_config_tier_alias_normalized` | `models={"haiku":..,"sonnet":..,"opus":..}` → 校验通过（归一 fast/mid/strong） |
| `test_config_default_in_providers` | `default` 不在 `providers` → AssertionError |
| `test_config_fallback_in_providers` | `fallback` 含未知 id → AssertionError |
| `test_config_chain_dedups` | `default=a, fallback=[a,b]` → `chain()==["a","b"]` |
| `test_config_key_only_env_name` | `ProviderConfig` 字段集含 `api_key_env`、**不含** `api_key`/明文 |
| `test_resolve_key_missing_env_raises_name_only` | env 未设 → AssertionError 消息含变量名、**不含** key 值 |
| `test_local_pricing_defaults_zero` | local 无 pricing → `pricing["strong"]==Pricing()`（全 0） |

#### B. 档映射 + 记账（`tests/llm/test_gateway_basics.py`）

| test 函数 | 注入 | 断言要点 |
|---|---|---|
| `test_resolve_model_maps_tier` | cfg | `resolve_model(cfg,"anthropic",ModelTier.STRONG)=="claude-opus-4-8"` |
| `test_resolve_model_alias` | cfg | `resolve_model(cfg,"anthropic",ModelTier.OPUS)` == STRONG 同值 |
| `test_gateway_charges_budget_tokens` | FakeProvider 返回 `usage=Usage(input=100,output=50)` | `budget.spent_tokens==150`（`billable()`） |
| `test_gateway_charges_budget_usd` | usage + pricing | `budget.spent_usd == usage.usd(pricing)` |
| `test_gateway_last_usage` | — | `gateway.last_usage()` == 最近返回的 usage |
| `test_gateway_usage_carries_provider` | — | 返回 `usage.provider==实际供应商 id` |

#### C. 退避重试（`tests/llm/test_gateway_retry.py`）

| test 函数 | 注入 | 断言要点 |
|---|---|---|
| `test_retry_on_rate_limit_then_ok` | `errors=[RateLimitError, RateLimitError, None]` | 第3次成功；`generate` 调用 Provider 3 次 |
| `test_retry_on_server_error` | `errors=[ServerError, None]` | 重试1次成功 |
| `test_retry_exhausts_then_fallback` | 主 provider `errors=[RateLimitError×max_attempts]`, fallback ok | 退避用尽→回退；返回 fallback Response |
| `test_no_retry_on_auth` | `errors=[ProviderError(error_class="auth_error")]` | 立即抛，**不重试不回退**；Provider 只调1次 |
| `test_no_retry_on_bad_request` | `errors=[ProviderError(error_class="bad_request")]` | 立即抛；不回退 |
| `test_backoff_sequence` | spy `sleep_backoff` | 退避序列 = base*2^attempt 封顶 max（jitter 固定0.5） |
| `test_backoff_jitter_capped` | jitter=True | 退避值 ≤ max_ms |

#### D. 回退链（`tests/llm/test_gateway_fallback.py`）

| test 函数 | 注入 | 断言要点 |
|---|---|---|
| `test_fallback_on_unavailable` | 主 `errors=[ProviderError("u",error_class="unavailable")]`, fallback ok | 返回 fallback Response；`usage.provider=="local_ollama"` |
| `test_fallback_skips_missing_sdk_provider` | 主 type 缺 SDK（不进 dict）, fallback ok | `providers.get(主) is None` → 顺到 fallback |
| `test_all_providers_fail_raises` | 全链失败 | 抛 `AllProvidersFailed`；`.chain==["a","b"]`；`.last` 是最后异常 |
| `test_fallback_records_actual_provider` | 回退成功 | 审计/usage 记 fallback 的 provider/model（HP9 可追） |

#### E. 能力降级（`tests/llm/test_gateway_degrade.py`）

| test 函数 | 注入 caps | 断言要点 |
|---|---|---|
| `test_degrade_tool_use_to_prompt` | `tool_use=False` | `degrade_plan.prompt_tool_protocol=True`；req.tools=None；system 含工具协议；解析出 `ToolCall` |
| `test_degrade_structured_strips_schema` | `structured_output=False` | `req.response_schema=None`；degraded 含 `structured_output->repair_retry` |
| `test_degrade_caching_none_noop` | `prompt_caching="none"` | 请求不变；`_cache_warn` 抑制告警 |
| `test_degrade_vision_unsupported_raises` | `vision=False` + image 消息 | 抛 `CapabilityUnsupported` |
| `test_degrade_parallel_no_change` | `parallel_tool_calls=False` | 请求不变，degraded 不含 parallel |
| `test_degrade_writes_marker` | tool_use=False | `resp.raw["_degraded"]` 含降级码（供 ToolLoop 落 note） |

#### F. 缓存命中告警（`tests/llm/test_cache.py`）

| test 函数 | 断言要点 |
|---|---|
| `test_render_prompt_passes_clean_prefix` | 无易变 token → 返回 (stable, dynamic) |
| `test_render_prompt_asserts_chapter_number` | stable 含「第12章」→ AssertionError |
| `test_render_prompt_asserts_uuid` | stable 含 uuid 片段 → AssertionError |
| `test_has_volatile_token_iso_timestamp` | ISO 时间戳 → True |
| `test_cache_warn_on_repeated_miss` | 同前缀第2次 `cache_read==0` + explicit → `log.warning`（caplog 断言） |
| `test_cache_warn_suppressed_for_local` | `prompt_caching=="none"` + `cache_read==0` → 无告警 |
| `test_cache_warn_silent_on_hit` | `cache_read>0` → 无告警 |

#### G. 结构化输出修复重试（`tests/llm/test_structured.py`）

| test 函数 | 脚本 | 断言要点 |
|---|---|---|
| `test_structured_first_pass` | script=[合法 JSON] | 一次过返回对象，Provider 只调1次 |
| `test_structured_repairs_then_ok` | script=[非法, 合法] | 第2次合法 → 返回对象；max_repair 内 |
| `test_structured_exhausts_raises` | script=[非法×(max_repair+1)] | 抛 `StructuredOutputError` |
| `test_structured_repair_charges_budget` | script=[非法,合法] | budget 累计两次调用的 usage（修复烧预算 HP10） |
| `test_structured_anthropic_tool_forced` | tool 强制路径 | `_extract_payload` 取 `tool_calls[0].args` 序列化 |
| `test_structured_repair_appends_error_msg` | 非法→合法 | 第2轮 messages 含「上次输出不满足 schema」 |

#### H. 工具调用 round-trip（三供应商各自译码，`tests/llm/test_tool_roundtrip.py`，多数 `@pytest.mark.integration`，解析单测用录制响应）

| test 函数 | 断言要点 |
|---|---|
| `test_anthropic_parse_tool_use_block` | 录制 `content[tool_use{id,name,input}]` → `ToolCall(args=input)`；request `input_schema==Tool.json_schema` |
| `test_anthropic_tool_result_message` | `ChatMessage(role=TOOL)` → `{"type":"tool_result","tool_use_id":...}` |
| `test_openai_parse_tool_calls_json_args` | `tool_calls[].function.arguments`(JSON 串) → `json.loads` 成 dict |
| `test_openai_request_function_params` | request `tools[].function.parameters==Tool.json_schema` |
| `test_ollama_parse_tool_calls_object_args` | `message.tool_calls[].function.arguments`（已是对象）→ 直接取 |
| `test_ollama_tool_call_generates_id` | 无 id → `new_id("tc")` 生成、非空 |
| `test_prompt_fallback_roundtrip` | caps.tool_use=False → render 进 system → 模型文本 JSON → `parse_tool_protocol` 还原同形 `ToolCall` |

#### I. 流式 assemble（`tests/llm/test_stream.py`）

| test 函数 | 脚本（ProviderStreamEvent 序列） | 断言要点 |
|---|---|---|
| `test_assemble_text_only` | [TEXT_DELTA×3, DONE(usage)] | `Response.text` 拼接正确；usage 来自 DONE |
| `test_assemble_tool_call_deltas` | [TOOL_CALL_DELTA(name)+partial×n, TOOL_CALL_DONE, DONE] | 装配出完整 `ToolCall`，args=拼接 JSON |
| `test_assemble_openai_incremental_args` | OpenAI 风格增量 arguments 串 | `json.loads` 拼接结果正确 |
| `test_assemble_malformed_args_empty` | 增量 JSON 不合法 | `ToolCall.args=={}`（容错） |
| `test_assemble_stop_reason_tool_use` | 含 TOOL_CALL_DONE | `stop_reason==TOOL_USE` |
| `test_gateway_stream_charges_on_done` | FakeProvider.stream | 末 DONE → `budget.charge` 调用一次 |
| `test_gateway_stream_no_cross_fallback` | default stream 抛异常 | 不切 fallback；异常上抛 |

#### J. pricing 归一化（`tests/llm/test_pricing.py`）

| test 函数 | 断言要点 |
|---|---|
| `test_normalize_anthropic_usage` | dict 含 4 字段 → `Usage` 对应映射 |
| `test_normalize_openai_usage_subtracts_cached` | prompt=100,cached=30 → `input==70, cache_read==30` |
| `test_normalize_ollama_usage` | `prompt_eval_count`/`eval_count` → input/output；cache_read=0 |
| `test_pricing_for_tier_alias` | `pricing_for(pc, ModelTier.OPUS)` == strong 价 |
| `test_getter_handles_sdk_object_and_dict` | 属性对象与 dict 都能取字段 |

#### K. key 不入日志断言（`tests/llm/test_no_key_leak.py`）

| test 函数 | 断言要点 |
|---|---|
| `test_response_raw_excluded_from_log` | `Response.raw` 即便置原始响应，ToolLoop 落 `tool_call_log` 只取 note；raw 不序列化进库 |
| `test_provider_config_dump_no_key` | `ProviderConfig.model_dump()` 不含任何 key 值（只有 `api_key_env`） |
| `test_resolve_key_error_omits_value` | env 缺失异常消息只含变量名 |
| `test_gateway_no_key_in_usage` | `Usage` 字段集不含任何 key/secret |
| `test_log_messages_have_no_key` | caplog 全量断言：退避/告警/降级日志均不含 key 值（用哨兵 key 注入 env，断言不出现于任何 log record） |

#### L. 契约测试（`tests/llm/test_provider_seam.py`，承接接缝篇 §7.3 H）

| test 函数 | 断言要点 |
|---|---|
| `test_fakeprovider_satisfies_protocol` | `isinstance(FakeProvider(...), LLMProvider)`（runtime_checkable） |
| `test_anthropic_optional_import` | 未装 anthropic：`import novelforge.llm.providers.anthropic` 不崩；构造抛 `ProviderError(error_class="unavailable")` |
| `test_openai_optional_import` | 同上（openai） |
| `test_local_works_without_httpx` | 未装 httpx：`LocalProvider` 可构造（urllib 兜底路径存在） |
| `test_gateway_generate_signature` | `inspect.signature(LLMGateway.generate)` 含 `tier/messages/system/tools/response_schema/cache_hint/max_tokens` |
| `test_no_import_tools_or_skills` | ast 扫描 `novelforge/llm/*`：无 `import novelforge.tools`/`novelforge.skills` |
| `test_seam_types_not_redefined_in_llm` | ast：本篇模块内无 `class ChatMessage/Usage/ToolCall/...`（只 import） |

---

## 14.13b 提示式工具协议的修复重试闭环（补遗）

§14.6 提到"本地模型无原生 tool_use 时走提示式 JSON 协议；连续解析不出合法 JSON 则按修复重试上限处理"，但 §14.9.5 `_post_process` 只解析一次即返回——此处补全修复重试落点，**置于 `Gateway.generate` 内、紧贴 `prompt_tool_protocol` 降级路径**（不是 provider 内部）：

```python
# novelforge/llm/gateway.py（generate 内，仅当本轮 degrade_plan 选了 prompt_tool_protocol 时）
def _generate_with_tool_protocol(self, provider, req, tier) -> Response:
    max_repair = self.cfg.structured.max_repair          # 复用结构化修复上限（默认 2）
    attempt = 0
    while True:
        resp = provider.generate(**req)                   # 退避/错误归一仍由 _call_with_retry 包裹
        self.budget.charge(resp.usage, self._pricing_for(provider.conf, tier))
        calls = parse_tool_protocol(resp.text)            # §14.6 花括号配平解析
        if calls or not _looks_like_tool_attempt(resp.text):
            # 解析出工具调用，或模型本就在给最终答案（无工具意图）→ 正常返回
            return resp.model_copy(update={"tool_calls": calls})
        if attempt >= max_repair:
            # 修复耗尽：当作"无工具调用、纯文本答复"返回，并打 degraded（B17 边界）
            return resp.model_copy(update={"tool_calls": [],
                       "raw": {**(resp.raw or {}), "_degraded": "tool_protocol_unparsable"}})
        # 追加一条修复提示（"上次输出不是合法工具 JSON，请严格按 schema 重发"），再试
        req = _append_repair_turn(req, resp.text)
        attempt += 1
```

要点：(1) 修复循环在 **Gateway 层**，provider 只管单次调用；(2) 上限复用 `config.providers.*.structured.max_repair`（不新增配置项）；(3) 每次重试照常 `budget.charge`（重试耗 token 计预算，HP10）；(4) 耗尽不抛错而是降级为"无工具调用纯文本 + `_degraded` 标记"，由 §12 ToolLoop 落 `tool_call_log.note` 并据"无 tool_calls 即终止"自然收尾，**不丢章**。

---

## 14.14 本规格与其他节的衔接

- **接缝四件套**：`ChatMessage/Tool/ToolCall/ToolResult/Response/Usage/Pricing/CacheHint/ProviderStreamEvent/CapabilitySet/ModelTier/异常树` 全部 `import` 自 `novelforge/llm/{types,errors,tiers}.py` 与 `novelforge/tools/errors.py`，本篇不重定义（接缝篇权威）。
- **§12 ToolLoop 调用**：`ToolLoop` 唯一调 `LLMGateway.generate(...)`（接缝篇 §4 调用契约）；拿回 `Response{text, tool_calls, usage(已 charge), stop_reason}`；`degraded` 经 `resp.raw["_degraded"]` 传递，ToolLoop 落 `tool_call_log.note`。LLM 故障（`LLMError` 系）由 Gateway 内部消化（退避/回退）或上抛 Orchestrator；不与 `ToolError` 交叉（接缝篇 §4.3 铁律）。
- **§07.6 BudgetLedger/CircuitBreaker**：`LLMGateway.__init__(cfg, budget)` 持 `BudgetLedger`；每次 `generate`/`stream`/结构化修复内 `budget.charge(usage)`（`LLMUsage=Usage`，`charge` 调 `billable()`/`usd(pricing)`）；退避耗时计会话预算；`breaker.guard()` 由 ToolLoop/Orchestrator 在调用点 guard，超限 `CircuitTripped`。
- **§02 存储**：审计落 `tool_call_log`（列 `provider`/`model`/`note`，§12.5 DDL）与 `skill_run_log`（列 `prompt_tokens`/`cache_read_tokens`/`output_tokens`/`usd_cost`，§07.3.1）。`generate_structured` 产 `BibleChangeProposal`（`novelforge/contracts.py`，§02.9/§10）入治理链。
- **§13 SSE**：`stream()` 产 `ProviderStreamEvent` 序列，经 Orchestrator 转译为 §13 业务级 SSE `StreamEvent`（两层不可混用）。
- **§08 config**：`config.providers` 段（§14.3）承接 §08 config 全集；`models` 段保留兼容。key 仅 env，config 文件可安全入 git。
