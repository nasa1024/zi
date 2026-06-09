"""API 安全层（§11 REST auth + prompt injection 防护）。

auth_middleware: Bearer / X-API-Key 头校验（NOVELFORGE_API_KEY 环境变量）
sanitize_user_text: prompt injection 净化（去除 system/instruction/role 注入模式）
"""
from __future__ import annotations

import os
import re
from typing import Optional


# ── Bearer / API-key 认证 ─────────────────────────────────────────────────────

_BYPASS_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})


def get_required_api_key() -> Optional[str]:
    """返回 NOVELFORGE_API_KEY（若已配置）。None 表示不强制认证（开发模式）。"""
    return os.environ.get("NOVELFORGE_API_KEY") or None


def check_api_key(authorization: Optional[str], x_api_key: Optional[str]) -> bool:
    """校验请求携带的 API key。

    接受两种格式：
    - Authorization: Bearer <key>
    - X-API-Key: <key>

    若未配置 NOVELFORGE_API_KEY 则始终通过（开发模式）。
    """
    required = get_required_api_key()
    if required is None:
        return True  # 开发模式，不强制认证

    provided: Optional[str] = None
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    elif x_api_key:
        provided = x_api_key.strip()

    return provided == required


# ── Prompt Injection 净化 ─────────────────────────────────────────────────────

# 常见注入模式：尝试重写系统提示、切换角色、泄露内部指令
_INJECTION_PATTERNS = [
    # 忽略/重写系统指令
    r"(?i)(ignore\s+(all\s+)?previous\s+instructions?)",
    r"(?i)(forget\s+(everything|all)\s+(above|before))",
    r"(?i)(disregard\s+(your\s+)?(system\s+)?prompt)",
    r"(?i)(你现在是|你是|you are now)\s*[\"']?[A-Z一-鿿]+[\"']?\s*[,.，。]",
    # 尝试输出系统提示
    r"(?i)(print|output|repeat|show|display)\s+(your\s+)?(system\s+)?(prompt|instruction)",
    r"(?i)(what\s+(are|is)\s+your\s+(instruction|prompt|system))",
    # 角色扮演注入
    r"(?i)(pretend\s+(to\s+be|you\s+are|you're)\s+)",
    r"(?i)(act\s+as\s+(if\s+you\s+are|a\s+))",
    # 中文注入模式
    r"(?i)(请忽略|忽略之前|忘记之前|你的真实身份)",
    r"(?i)(系统提示|system\s*prompt|internal\s+instruction)",
    r"(?i)(DAN\s*mode|越狱|jailbreak)",
]

_COMPILED = [re.compile(p) for p in _INJECTION_PATTERNS]

_INJECTION_PLACEHOLDER = "[已净化]"


def sanitize_user_text(text: str, max_length: int = 10000) -> str:
    """净化用户输入文本，防止 prompt injection。

    1. 长度截断（防止超长输入撑爆上下文）
    2. 移除已知注入模式，替换为占位符
    """
    if not text:
        return text

    # 1. 长度截断
    if len(text) > max_length:
        text = text[:max_length] + "...[截断]"

    # 2. 注入模式替换
    for pattern in _COMPILED:
        text = pattern.sub(_INJECTION_PLACEHOLDER, text)

    return text
