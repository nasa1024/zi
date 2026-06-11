"""LLMProvider Protocol 及共享类型（§14.2）。

具体实现（AnthropicProvider / FakeProvider）见同包其他模块。
Gateway 仅依赖本文件，不直接依赖 SDK。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class Message:
    role: str                       # "user" | "assistant" | "system"
    content: str | list             # 纯文本 或 content blocks 列表


@dataclass
class CacheHint:
    """Prompt caching 标记（Anthropic cache_control；DeepSeek 端自动前缀缓存、忽略本提示）。

    breakpoint        : system 块的 cache_control 类型
    user_prefix_chars : 首条 user 消息的前 N 个字符为稳定前缀（M1-⑥ stable_context），
                        provider 将其切为独立 content block 并标记 cache_control；
                        0 = 不切。
    """
    breakpoint: str = "ephemeral"   # "ephemeral" | "persistent"
    user_prefix_chars: int = 0


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    tool_use_id: str
    name: str
    input: dict


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    provider: str = ""
    model: str = ""

    @property
    def total(self) -> int:
        return self.input + self.output


@dataclass
class Response:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = "end_turn"    # "end_turn" | "tool_use" | "max_tokens"
    raw: Any = None                  # 原始 SDK 响应（调试用）


@dataclass
class CapabilitySet:
    supports_tools: bool = True
    supports_cache: bool = False
    max_tokens_out: int = 8192
    context_window: int = 128_000   # 上下文窗口大小（tokens）


@runtime_checkable
class LLMProvider(Protocol):
    """统一 provider 接口——所有实现须满足此协议。"""

    def generate(
        self,
        messages: list[Message],
        *,
        model: str,
        max_tokens: int = 4096,
        system: Optional[str] = None,
        tools: Optional[list[Tool]] = None,
        temperature: float = 1.0,
        cache_hint: Optional[CacheHint] = None,
    ) -> Response: ...

    def capabilities(self, model: str) -> CapabilitySet: ...
