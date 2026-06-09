"""OpenAICompatProvider：兼容 OpenAI Chat Completions 格式的通用 provider。

适用于 DeepSeek / 本地 Ollama / vLLM / 任何 OpenAI-compat 端点。
依赖：pip install openai>=1.0
"""
from __future__ import annotations

from typing import Optional

from .provider import CapabilitySet, CacheHint, Message, Response, Tool, ToolCall, Usage

try:
    from openai import OpenAI as _OpenAI
    _HAS_OPENAI = True
except ImportError:
    _HAS_OPENAI = False


def _to_sdk_messages(messages: list[Message]) -> list[dict]:
    out = []
    for m in messages:
        out.append({"role": m.role, "content": m.content})
    return out


def _tool_to_sdk(t: Tool) -> dict:
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.input_schema,
        },
    }


class OpenAICompatProvider:
    """OpenAI-compatible API provider（DeepSeek / Ollama / vLLM 均适用）。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        **kwargs,
    ) -> None:
        if not _HAS_OPENAI:
            raise ImportError(
                "openai 包未安装。运行: pip install openai>=1.0"
            )
        self._client = _OpenAI(api_key=api_key or "placeholder", base_url=base_url)
        self._base_url = base_url or ""

    def generate(
        self,
        messages: list[Message],
        *,
        model: str,
        max_tokens: int = 4096,
        system: Optional[str] = None,
        tools: Optional[list[Tool]] = None,
        temperature: float = 1.0,
        cache_hint: Optional[CacheHint] = None,  # OpenAI-compat 暂不支持 cache_hint，忽略
    ) -> Response:
        sdk_messages = []
        if system:
            sdk_messages.append({"role": "system", "content": system})
        sdk_messages.extend(_to_sdk_messages(messages))

        kwargs: dict = dict(
            model=model,
            messages=sdk_messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if tools:
            kwargs["tools"] = [_tool_to_sdk(t) for t in tools]
            kwargs["tool_choice"] = "auto"

        resp = self._client.chat.completions.create(**kwargs)

        choice = resp.choices[0]
        msg = choice.message
        text = msg.content or ""
        # DeepSeek V4 Pro 等 thinking 模型：reasoning_content 是链式思维，
        # content 才是最终回答。两者都存入 raw，text 只取 content。
        reasoning = getattr(msg, "reasoning_content", None) or ""

        tool_calls: list[ToolCall] = []
        if msg.tool_calls:
            import json
            for tc in msg.tool_calls:
                tool_calls.append(ToolCall(
                    tool_use_id=tc.id,
                    name=tc.function.name,
                    input=json.loads(tc.function.arguments or "{}"),
                ))

        u = resp.usage
        usage = Usage(
            input=u.prompt_tokens if u else 0,
            output=u.completion_tokens if u else 0,
            cache_read=getattr(u, "prompt_cache_hit_tokens", 0) or 0,
            cache_write=getattr(u, "prompt_cache_miss_tokens", 0) or 0,
            provider="openai_compat",
            model=model,
        )
        stop = choice.finish_reason or "end_turn"
        # reasoning 存入 raw_extra，供调试/日志使用
        return Response(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=stop,
            raw={"resp": resp, "reasoning": reasoning},
        )

    # DeepSeek V4 能力参数
    _CAPS = {
        "deepseek-v4-pro":   CapabilitySet(supports_tools=True, supports_cache=False,
                                            max_tokens_out=384_000, context_window=1_000_000),
        "deepseek-v4-flash": CapabilitySet(supports_tools=True, supports_cache=False,
                                            max_tokens_out=384_000, context_window=1_000_000),
        "deepseek-chat":     CapabilitySet(supports_tools=True, supports_cache=False,
                                            max_tokens_out=8192, context_window=64_000),
        "deepseek-reasoner": CapabilitySet(supports_tools=False, supports_cache=False,
                                            max_tokens_out=8192, context_window=64_000),
    }

    def capabilities(self, model: str) -> CapabilitySet:
        return self._CAPS.get(
            model,
            CapabilitySet(supports_tools=True, supports_cache=False,
                          max_tokens_out=8192, context_window=128_000),
        )
