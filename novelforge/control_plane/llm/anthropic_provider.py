"""AnthropicProvider：封装 anthropic SDK（§14.3）。

依赖可选：`pip install anthropic>=0.40`
未安装时实例化抛 ImportError，而非导入时崩溃。
"""
from __future__ import annotations

from .provider import CapabilitySet, CacheHint, Message, Response, Tool, ToolCall, Usage

try:
    import anthropic as _sdk
    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False


def _to_sdk_messages(messages: list[Message]) -> list[dict]:
    out = []
    for m in messages:
        if m.role == "system":
            continue  # system 通过顶层 system= 参数传
        out.append({"role": m.role, "content": m.content})
    return out


def _apply_user_prefix_cache(sdk_messages: list[dict], prefix_chars: int, breakpoint: str) -> None:
    """把首条 user 消息的前 prefix_chars 字符切为独立 block 并标 cache_control（M1-⑥）。

    仅当 content 是 str 且长于前缀时生效；原地修改。
    """
    if prefix_chars <= 0:
        return
    for m in sdk_messages:
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str) and len(content) > prefix_chars:
            m["content"] = [
                {"type": "text", "text": content[:prefix_chars],
                 "cache_control": {"type": breakpoint}},
                {"type": "text", "text": content[prefix_chars:]},
            ]
        return  # 只处理第一条 user 消息


def _tool_to_sdk(t: Tool) -> dict:
    return {
        "name": t.name,
        "description": t.description,
        "input_schema": t.input_schema,
    }


class AnthropicProvider:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        **kwargs,
    ) -> None:
        if not _HAS_SDK:
            raise ImportError(
                "anthropic 包未安装。运行: pip install anthropic>=0.40"
            )
        init_kw: dict = {"api_key": api_key}
        if base_url:
            init_kw["base_url"] = base_url
        self._client = _sdk.Anthropic(**init_kw)
        self._base_url = base_url or ""

    def generate(
        self,
        messages: list[Message],
        *,
        model: str,
        max_tokens: int = 4096,
        system: str | None = None,
        tools: list[Tool] | None = None,
        temperature: float = 1.0,
        cache_hint: CacheHint | None = None,
    ) -> Response:
        sdk_kw: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": _to_sdk_messages(messages),
        }
        if cache_hint and cache_hint.user_prefix_chars > 0:
            _apply_user_prefix_cache(
                sdk_kw["messages"], cache_hint.user_prefix_chars, cache_hint.breakpoint
            )
        # system prompt（可附 cache_control）
        if system:
            if cache_hint:
                sdk_kw["system"] = [
                    {"type": "text", "text": system,
                     "cache_control": {"type": cache_hint.breakpoint}}
                ]
            else:
                sdk_kw["system"] = system
        if tools:
            sdk_kw["tools"] = [_tool_to_sdk(t) for t in tools]
        if temperature != 1.0:
            sdk_kw["temperature"] = temperature

        resp = self._client.messages.create(**sdk_kw)

        text = ""
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text += block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    tool_use_id=block.id,
                    name=block.name,
                    input=block.input,
                ))

        u = resp.usage
        usage = Usage(
            input=u.input_tokens,
            output=u.output_tokens,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
            provider="anthropic",
            model=model,
        )
        return Response(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=resp.stop_reason,
            raw=resp,
        )

    # DeepSeek Anthropic 端点的能力（base_url 含 deepseek.com 时生效）
    _DEEPSEEK_CAPS = {
        "deepseek-v4-pro":   CapabilitySet(supports_tools=True, supports_cache=True,
                                            max_tokens_out=384_000, context_window=1_000_000),
        "deepseek-v4-flash": CapabilitySet(supports_tools=True, supports_cache=True,
                                            max_tokens_out=384_000, context_window=1_000_000),
    }

    def capabilities(self, model: str) -> CapabilitySet:
        if "deepseek.com" in self._base_url:
            # 通过 Anthropic 格式访问 DeepSeek 端点
            if model in self._DEEPSEEK_CAPS:
                return self._DEEPSEEK_CAPS[model]
            # Claude 名称被 DeepSeek 自动映射时也返回 V4 Pro 能力
            if model.startswith("claude-opus"):
                return self._DEEPSEEK_CAPS["deepseek-v4-pro"]
            return self._DEEPSEEK_CAPS["deepseek-v4-flash"]
        # 标准 Anthropic 端点
        return CapabilitySet(
            supports_tools=True,
            supports_cache=True,
            max_tokens_out=8192,
            context_window=200_000,
        )
