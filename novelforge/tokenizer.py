"""FTS5 应用层预分词（设计 §2.8）。

设计要求中文用 jieba 预分词后空格 join 写入 FTS5（unicode61 按空白切分）。
jieba 是可选依赖：未安装时回退到「单字 + bigram」分词，保证 MVP0 零外部依赖可跑，
召回质量略降但功能完整。tokenizer_version 写入 meta_kv 以支持按版本重建（§2.1）。
"""
from __future__ import annotations

import hashlib
import re

try:  # 可选依赖
    import jieba  # type: ignore

    _HAS_JIEBA = True
except Exception:  # pragma: no cover - 取决于环境
    jieba = None
    _HAS_JIEBA = False

_CJK = re.compile(r"[一-鿿]")
_WORD = re.compile(r"[A-Za-z0-9]+")
_USER_TERMS: set[str] = set()


def add_user_terms(terms) -> None:
    """注入专名词典（人名/境界/地名等，来自 entities/aliases/power_ranks，§2.8 关键工程点3）。"""
    for t in terms:
        t = (t or "").strip()
        if not t:
            continue
        _USER_TERMS.add(t)
        if _HAS_JIEBA:
            jieba.add_word(t)  # type: ignore


def _fallback_cut(text: str):
    """无 jieba 时的确定性分词：英数整词 + 中文单字 + 中文 bigram + 命中的专名整词。"""
    toks: list[str] = []
    # 专名整词优先（保证 '林枫'/'筑基期' 作为整体可召回）
    for term in _USER_TERMS:
        if term and term in text:
            toks.append(term)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if _CJK.match(ch):
            toks.append(ch)
            if i + 1 < n and _CJK.match(text[i + 1]):
                toks.append(text[i] + text[i + 1])  # bigram
            i += 1
        else:
            m = _WORD.match(text, i)
            if m:
                toks.append(m.group(0).lower())
                i = m.end()
            else:
                i += 1
    return toks


def cut(text: str) -> list[str]:
    if not text:
        return []
    if _HAS_JIEBA:
        return [t for t in jieba.lcut(text) if t.strip()]  # type: ignore
    return _fallback_cut(text)


def tokenize(text: str) -> str:
    """返回空格分隔的词串，写入 FTS5 / 构造 MATCH 查询（query 与 doc 同口径）。"""
    return " ".join(cut(text or ""))


def tokenizer_version() -> str:
    """jieba 版本 + 专名词典哈希（§2.1 版本绑定，写入 meta_kv）。"""
    base = f"jieba-{getattr(jieba, '__version__', 'none')}" if _HAS_JIEBA else "fallback-bigram-1"
    dict_sha = hashlib.sha256("".join(sorted(_USER_TERMS)).encode("utf-8")).hexdigest()[:12]
    return f"{base}+dict:{dict_sha}"
