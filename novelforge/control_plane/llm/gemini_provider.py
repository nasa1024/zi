"""GeminiProvider：Google Gemini API（generativelanguage v1beta REST）。

面向 Gemini 4 Argon（``gemini-4-argon``）设计，零第三方依赖（标准库 urllib + SSE）：

- 输入与输出共享 1,048,576 token 统一上限；单次流式请求最多产出 65,536 token。
  超出部分服务端返回 ``finishReason=CONTINUATION`` + ``continuationToken``，本
  provider 原样重发请求并附上 token 续接（long decode），对上层表现为一次完整调用。
- 思考强度用 ``thinkingLevel``（low / medium / high，默认 medium）；Gemini 4 对
  ``thinkingBudget`` 与 ``minimal`` 返回 400。模型 ID 可写成 ``gemini-4-argon@high``
  为某一档位单独指定思考强度（gateway 三档可映射到同一模型的不同思考强度）。
- 思考 token 计入 ``maxOutputTokens`` 并按输出计费：调用方给的 ``max_tokens`` 视为
  可见正文预算，本 provider 按思考强度在其上追加余量，避免推理吃光正文预算。
- ``temperature`` / ``top_p`` / ``top_k`` 在 Gemini 3+/4 上被忽略，故不下发。
- 不支持显式缓存：≥4,096 token（Vertex 为 6,144）的共享前缀自动隐式缓存（95% 折扣）。system 与稳定
  上下文本就排在最前，``CacheHint`` 无需切块；命中量记入 ``Usage.cache_read``。
- 不支持以 model 回合结尾的预填充对话。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Iterable, Iterator, Optional

from .provider import CapabilitySet, CacheHint, Message, Response, Tool, ToolCall, Usage

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com"
GEMINI_API_VERSION = "v1beta"

THINKING_LEVELS = ("low", "medium", "high")

# 思考 token 计入 maxOutputTokens：在调用方可见输出预算之上追加的余量（未用部分不计费）
_THINKING_HEADROOM = {"low": 4_096, "medium": 16_384, "high": 32_768}

_REQUEST_TIMEOUT_S = 20 * 60    # 服务端单请求上限 20 分钟
_MAX_HOPS = 64                  # 续接跳数保护：1M / 65,536 ≈ 16 跳，留足余量
_MAX_BUDGET_EXTENSIONS = 1      # 思考耗尽预算且尚无正文时，自动加倍 maxOutputTokens 的次数

_SAFETY_CATEGORIES = (
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
)
SAFETY_THRESHOLDS = (
    "BLOCK_NONE", "BLOCK_ONLY_HIGH", "BLOCK_MEDIUM_AND_ABOVE", "BLOCK_LOW_AND_ABOVE", "OFF",
)

_STOP_REASONS = {"STOP": "end_turn", "MAX_TOKENS": "max_tokens", "CONTINUATION": "max_tokens"}
_REFUSAL_REASONS = {
    "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY",
}

_GEMINI4_CAPS = CapabilitySet(
    supports_tools=True, supports_cache=True,
    max_tokens_out=1_048_576, context_window=1_048_576, long_output=True,
)
_GEMINI_DEFAULT_CAPS = CapabilitySet(
    supports_tools=True, supports_cache=True,
    max_tokens_out=65_536, context_window=1_048_576,
)

# transport(url, headers, body, timeout) -> 逐个产出响应 JSON（流式分片）
Transport = Callable[[str, dict, dict, float], Iterable[dict]]


class GeminiAPIError(RuntimeError):
    """Gemini HTTP / 流内错误。消息含状态码，供 gateway 的可重试判定（429/5xx）。"""

    def __init__(self, code: int | str, status: str, message: str) -> None:
        super().__init__(f"gemini HTTP {code} {status}: {message}")
        self.code = code
        self.status = status


def split_model_id(model: str) -> tuple[str, Optional[str]]:
    """``"gemini-4-argon@high"`` → ``("gemini-4-argon", "high")``；无后缀时 level 为 None。"""
    base, sep, level = model.partition("@")
    level = level.strip().lower()
    if sep and level not in THINKING_LEVELS:
        raise ValueError(
            f"未知思考强度 {level!r}（模型 {model!r}）；可选: {', '.join(THINKING_LEVELS)}"
        )
    return base.strip(), (level or None)


def _is_gemini4(base_model: str) -> bool:
    return base_model.startswith("gemini-4")


def _ignores_sampling(base_model: str) -> bool:
    """Gemini 3+/4 忽略 temperature/top_p/top_k。"""
    return base_model.startswith(("gemini-3", "gemini-4"))


def _plain_text(content: str | list) -> str:
    if isinstance(content, str):
        return content
    out = []
    for block in content:
        if isinstance(block, str):
            out.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            out.append(block.get("text", ""))
    return "".join(out)


def _to_parts(content: str | list, tool_names: dict[str, str]) -> list[dict]:
    """Message.content（纯文本或 Anthropic 风格 content blocks）→ Gemini parts。"""
    if isinstance(content, str):
        return [{"text": content}] if content else []
    parts: list[dict] = []
    for block in content:
        if isinstance(block, str):
            parts.append({"text": block})
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append({"text": block.get("text", "")})
        elif kind == "tool_use":
            tool_names[block["id"]] = block["name"]
            parts.append({"functionCall": {
                "id": block["id"], "name": block["name"], "args": block.get("input") or {},
            }})
        elif kind == "tool_result":
            call_id = block.get("tool_use_id", "")
            parts.append({"functionResponse": {
                "id": call_id,
                "name": tool_names.get(call_id, ""),
                "response": {"result": _plain_text(block.get("content") or "")},
            }})
    return parts


def _to_contents(messages: list[Message]) -> tuple[list[dict], list[str]]:
    """返回 (contents, 内联 system 文本)。同角色相邻消息合并为一个回合。"""
    contents: list[dict] = []
    inline_system: list[str] = []
    tool_names: dict[str, str] = {}
    for m in messages:
        if m.role == "system":
            inline_system.append(_plain_text(m.content))
            continue
        role = "model" if m.role == "assistant" else "user"
        parts = _to_parts(m.content, tool_names)
        if not parts:
            continue
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})
    return contents, inline_system


def _tool_to_api(t: Tool) -> dict:
    return {
        "name": t.name,
        "description": t.description,
        "parametersJsonSchema": t.input_schema,
    }


def _urllib_transport(url: str, headers: dict, body: dict, timeout: float) -> Iterator[dict]:
    """POST 并按 SSE 解析流式响应（``data: {...}`` 事件，空行分隔）。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw).get("error") or {}
        except (ValueError, AttributeError):
            err = {}
        raise GeminiAPIError(
            e.code, err.get("status", ""), err.get("message") or raw[:500]
        ) from None
    with resp:
        data_lines: list[str] = []
        for raw_line in resp:
            line = raw_line.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif not line and data_lines:
                yield json.loads("\n".join(data_lines))
                data_lines = []
        if data_lines:
            yield json.loads("\n".join(data_lines))


class _HopResult:
    """单跳（一次 HTTP 请求）聚合结果。"""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.tool_calls: list[ToolCall] = []
        self.finish_reason = ""
        self.continuation_token: Optional[str] = None
        self.block_reason = ""
        self.usage: dict = {}

    def consume(self, chunk: dict) -> None:
        if "error" in chunk:
            err = chunk["error"] or {}
            raise GeminiAPIError(
                err.get("code", "stream"), err.get("status", ""), err.get("message", "")
            )
        feedback = chunk.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            self.block_reason = feedback["blockReason"]
        candidates = chunk.get("candidates") or []
        if candidates:
            cand = candidates[0]
            for part in (cand.get("content") or {}).get("parts") or []:
                if part.get("thought"):
                    continue            # 思考摘要不进正文
                if "text" in part:
                    self.text.append(part["text"])
                elif "functionCall" in part:
                    fc = part["functionCall"]
                    self.tool_calls.append(ToolCall(
                        tool_use_id=fc.get("id") or f"call_{len(self.tool_calls)}",
                        name=fc.get("name", ""),
                        input=fc.get("args") or {},
                    ))
            if cand.get("finishReason"):
                self.finish_reason = cand["finishReason"]
            if cand.get("continuationToken"):
                self.continuation_token = cand["continuationToken"]
        if chunk.get("usageMetadata"):
            # 流式分片中的用量是本跳累计值，取最后一个
            self.usage = chunk["usageMetadata"]


class GeminiProvider:
    """Google Gemini API provider（Gemini 4 long decode / 思考强度 / 隐式缓存）。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        *,
        safety_threshold: Optional[str] = None,
        timeout: float = _REQUEST_TIMEOUT_S,
        transport: Optional[Transport] = None,
        **kwargs,
    ) -> None:
        if not api_key:
            raise ValueError("缺少 Gemini API key：请设置 GEMINI_API_KEY")
        if safety_threshold and safety_threshold.upper() not in SAFETY_THRESHOLDS:
            raise ValueError(
                f"未知安全阈值 {safety_threshold!r}；可选: {', '.join(SAFETY_THRESHOLDS)}"
            )
        self._api_key = api_key
        self._base_url = (base_url or GEMINI_BASE_URL).rstrip("/")
        self._safety_threshold = safety_threshold.upper() if safety_threshold else None
        self._timeout = timeout
        self._transport = transport or _urllib_transport

    def _max_output(
        self, base_model: str, level: Optional[str], max_tokens: int, est_input: int,
    ) -> int:
        caps = self.capabilities(base_model)
        requested = max(1, max_tokens) + _THINKING_HEADROOM[level or "medium"]
        limit = caps.max_tokens_out
        if _is_gemini4(base_model):
            # 输入输出共享统一上限：给输入留位，同时不低于调用方要求的正文预算
            limit = min(limit, max(max_tokens, caps.context_window - est_input))
        return max(1, min(requested, limit))

    def _build_body(
        self,
        messages: list[Message],
        *,
        base_model: str,
        level: Optional[str],
        max_tokens: int,
        system: Optional[str],
        tools: Optional[list[Tool]],
        temperature: float,
    ) -> dict:
        contents, inline_system = _to_contents(messages)
        if not contents:
            raise ValueError("Gemini 请求至少需要一条 user 消息")
        if _is_gemini4(base_model) and contents[-1]["role"] == "model":
            raise ValueError("Gemini 4 不支持以 assistant 回合结尾的预填充对话")
        system_text = "\n\n".join(s for s in [system or "", *inline_system] if s)
        est_input = len(system_text) + sum(len(str(m.content)) for m in messages)

        gen_cfg: dict = {
            "maxOutputTokens": self._max_output(base_model, level, max_tokens, est_input),
        }
        if level:
            gen_cfg["thinkingConfig"] = {"thinkingLevel": level}
        if temperature != 1.0 and not _ignores_sampling(base_model):
            gen_cfg["temperature"] = temperature

        body: dict = {"contents": contents, "generationConfig": gen_cfg}
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}
        if tools:
            body["tools"] = [{"functionDeclarations": [_tool_to_api(t) for t in tools]}]
        if self._safety_threshold:
            body["safetySettings"] = [
                {"category": c, "threshold": self._safety_threshold} for c in _SAFETY_CATEGORIES
            ]
        return body

    def generate(
        self,
        messages: list[Message],
        *,
        model: str,
        max_tokens: int = 4096,
        system: Optional[str] = None,
        tools: Optional[list[Tool]] = None,
        temperature: float = 1.0,
        cache_hint: Optional[CacheHint] = None,  # 隐式缓存自动生效，无需切块
    ) -> Response:
        base_model, level = split_model_id(model)
        body = self._build_body(
            messages, base_model=base_model, level=level, max_tokens=max_tokens,
            system=system, tools=tools, temperature=temperature,
        )
        url = (
            f"{self._base_url}/{GEMINI_API_VERSION}/models/{base_model}"
            ":streamGenerateContent?alt=sse"
        )
        headers = {"Content-Type": "application/json", "x-goog-api-key": self._api_key}
        out_cap = self.capabilities(base_model).max_tokens_out

        text: list[str] = []
        tool_calls: list[ToolCall] = []
        usage = Usage(provider="gemini", model=model)
        hops: list[dict] = []
        token: Optional[str] = None
        extensions = 0
        finish = ""
        block_reason = ""
        for _ in range(_MAX_HOPS):
            request = dict(body)
            if token:
                # 续接：原样重发原始请求 + 顶层 continuationToken（不要把已生成文本拼回 contents）
                request["continuationToken"] = token
            hop = _HopResult()
            for chunk in self._transport(url, headers, request, self._timeout):
                hop.consume(chunk)
            text.extend(hop.text)
            tool_calls.extend(hop.tool_calls)
            u = hop.usage
            usage.input += (u.get("promptTokenCount") or 0) + (u.get("toolUsePromptTokenCount") or 0)
            usage.output += (u.get("candidatesTokenCount") or 0) + (u.get("thoughtsTokenCount") or 0)
            usage.cache_read += u.get("cachedContentTokenCount") or 0
            hops.append({"finish_reason": hop.finish_reason, "usage": u})
            finish, token = hop.finish_reason, hop.continuation_token
            block_reason = hop.block_reason or block_reason
            if finish == "CONTINUATION" and token:
                continue
            gen_cfg = body["generationConfig"]
            if (finish == "MAX_TOKENS" and token and not "".join(text).strip()
                    and extensions < _MAX_BUDGET_EXTENSIONS
                    and gen_cfg["maxOutputTokens"] < out_cap):
                # 思考耗尽预算、尚无正文：maxOutputTokens 跨跳累计且须单调递增，加倍后续接
                body = {**body, "generationConfig": {
                    **gen_cfg, "maxOutputTokens": min(out_cap, gen_cfg["maxOutputTokens"] * 2),
                }}
                extensions += 1
                continue
            break

        if tool_calls:
            stop = "tool_use"
        elif block_reason or finish in _REFUSAL_REASONS:
            stop = "refusal"
        else:
            stop = _STOP_REASONS.get(finish, finish.lower() or "end_turn")
        return Response(
            text="".join(text),
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=stop,
            raw={"hops": hops, "block_reason": block_reason or None},
        )

    def capabilities(self, model: str) -> CapabilitySet:
        base_model = model.partition("@")[0].strip()
        if _is_gemini4(base_model):
            return _GEMINI4_CAPS
        return _GEMINI_DEFAULT_CAPS
