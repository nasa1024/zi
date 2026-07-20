"""Language-aware application tokenization for SQLite FTS5.

Chinese keeps the historical jieba (or character/bigram) path.  English is
never sent through jieba: it uses a deterministic Unicode Latin-word scanner
and preserves apostrophes/hyphens while deciding word boundaries.  The final
FTS representation removes those connectors because SQLite's ``unicode61``
tokenizer treats them as separators; queries use the same representation.

``tokenizer_version`` includes the content language so an English index can
never be mistaken for a Chinese one.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable

try:  # 可选依赖
    import jieba  # type: ignore

    _HAS_JIEBA = True
except Exception:  # pragma: no cover - 取决于环境
    jieba = None
    _HAS_JIEBA = False

_CJK = re.compile(r"[一-鿿]")
_WORD = re.compile(r"[A-Za-z0-9]+")
_USER_TERMS: set[str] = set()
_USER_TERMS_BY_LANGUAGE: dict[str, set[str]] = {"zh-CN": _USER_TERMS}
_EN_CONNECTORS = {"'", "’", "-", "‐", "‑", "‒", "–", "—"}
_EN_COMPOUND_STOPWORDS = {"a", "an", "and", "for", "from", "in", "of", "the", "to"}
_EN_IRREGULAR_LEMMAS = {
    "did": "do", "does": "do", "done": "do",
    "gave": "give", "given": "give", "gone": "go", "went": "go",
    "had": "have", "has": "have", "made": "make", "ran": "run",
    "said": "say", "taken": "take", "took": "take",
    "was": "be", "were": "be", "wrote": "write", "written": "write",
}
_EN_SILENT_E_STEMS = (
    "com", "creat", "driv", "giv", "hop", "lik", "lov", "mak", "mov",
    "not", "tak", "us", "writ",
)


def _normalize_language(language: str | None) -> str:
    value = (language or "zh-CN").strip()
    return value or "zh-CN"


def _normalize_term(term: object, language: str) -> str:
    value = unicodedata.normalize("NFKC", str(term or "")).strip()
    return value.casefold() if language.startswith("en") else value


def add_user_terms(terms: Iterable[object], language: str = "zh-CN") -> None:
    """Add runtime terms to the matching language bucket.

    Project indexing passes an explicit term set and therefore does not rely
    on this process-global compatibility bucket.  The public function remains
    for the existing admin hot-update endpoint.
    """
    language = _normalize_language(language)
    bucket = _USER_TERMS_BY_LANGUAGE.setdefault(language, set())
    for t in terms:
        t = _normalize_term(t, language)
        if not t:
            continue
        bucket.add(t)
        if language == "zh-CN" and _HAS_JIEBA:
            jieba.add_word(t)  # type: ignore


def _terms_for(language: str, user_terms: Iterable[object] | None) -> set[str]:
    if user_terms is None:
        return set(_USER_TERMS_BY_LANGUAGE.get(language, ()))
    return {
        value for term in user_terms
        if (value := _normalize_term(term, language))
    }


def _fallback_cut(text: str, user_terms: Iterable[object] | None = None):
    """无 jieba 时的确定性分词：英数整词 + 中文单字 + 中文 bigram + 命中的专名整词。"""
    toks: list[str] = []
    # 专名整词优先（保证 '林枫'/'筑基期' 作为整体可召回）
    terms = _USER_TERMS if user_terms is None else {
        value for term in user_terms
        if (value := _normalize_term(term, "zh-CN"))
    }
    for term in terms:
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


def _is_latin_or_digit(ch: str) -> bool:
    if ch.isdigit():
        return True
    return ch.isalpha() and "LATIN" in unicodedata.name(ch, "")


def _english_cut(text: str) -> list[str]:
    """Return normalized Latin words without admitting CJK into English FTS."""
    source = unicodedata.normalize("NFKC", text)
    tokens: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if not buf:
            return
        value = "".join(buf).strip("'-").casefold()
        if value:
            tokens.append(value)
        buf.clear()

    for index, ch in enumerate(source):
        if _is_latin_or_digit(ch):
            buf.append(ch)
            continue
        if (
            ch in _EN_CONNECTORS
            and buf
            and index + 1 < len(source)
            and _is_latin_or_digit(source[index + 1])
        ):
            buf.append("'" if ch in {"'", "’"} else "-")
            continue
        flush()
    flush()
    return tokens


def cut(
    text: str,
    language: str = "zh-CN",
    *,
    user_terms: Iterable[object] | None = None,
) -> list[str]:
    if not text:
        return []
    language = _normalize_language(language)
    if language.startswith("en"):
        return _english_cut(text)
    if _HAS_JIEBA:
        # jieba's dictionary is process-global.  Register only Chinese terms;
        # English projects never reach this branch.
        for term in _terms_for(language, user_terms):
            jieba.add_word(term)  # type: ignore
        return [t for t in jieba.lcut(text) if t.strip()]  # type: ignore
    return _fallback_cut(text, _terms_for(language, user_terms))


def _english_fts_tokens(token: str) -> list[str]:
    """Encode a word for unicode61 while retaining compound recall.

    The joined form makes an exact hyphen/apostrophe query deterministic;
    component forms let ``Mara`` find ``Mara's`` and ``state`` find
    ``state-of-the-art``.
    """
    values = [token]
    values.extend(
        part for part in re.split(r"['-]+", token)
        if len(part) >= 3 and part not in _EN_COMPOUND_STOPWORDS
    )
    out: list[str] = []
    for value in values:
        safe = "".join(ch for ch in value if ch.isalnum() or ch == "_")
        if not safe:
            continue
        for normalized in (safe, *_english_lemmas(safe)):
            if normalized and normalized not in out:
                out.append(normalized)
    return out


def _english_lemmas(token: str) -> list[str]:
    """Return conservative search lemmas while retaining the surface token.

    This is deliberately smaller than a linguistic stemmer: it handles the
    common inflections needed for fiction recall without external models or
    locale-dependent dictionaries.  Both document and MATCH query paths call
    this function through ``tokenize``.
    """
    if not token.isalpha() or len(token) < 4:
        return []
    if irregular := _EN_IRREGULAR_LEMMAS.get(token):
        return [irregular]

    lemma = ""
    if token.endswith("ies") and len(token) > 4:
        lemma = token[:-3] + "y"
    elif token.endswith("ied") and len(token) > 4:
        lemma = token[:-3] + "y"
    elif token.endswith("ing") and len(token) > 5:
        stem = token[:-3]
        if len(stem) >= 3 and stem[-1] == stem[-2] and stem[-1] not in "lsz":
            lemma = stem[:-1]
        elif stem in {"dy", "ly", "ty"}:
            lemma = stem[:-1] + "ie"
        elif stem.endswith(_EN_SILENT_E_STEMS):
            lemma = stem + "e"
        else:
            lemma = stem
    elif token.endswith("ed") and len(token) > 4:
        stem = token[:-2]
        if len(stem) >= 3 and stem[-1] == stem[-2] and stem[-1] not in "lsz":
            lemma = stem[:-1]
        elif stem.endswith(_EN_SILENT_E_STEMS):
            lemma = stem + "e"
        else:
            lemma = stem
    elif token.endswith("es") and len(token) > 4 and token.endswith(
        ("sses", "shes", "ches", "xes", "zes", "oes")
    ):
        lemma = token[:-2]
    elif token.endswith("s") and not token.endswith(("ss", "us", "is")):
        lemma = token[:-1]

    return [lemma] if len(lemma) >= 3 and lemma != token else []


def tokenize(
    text: str,
    language: str = "zh-CN",
    *,
    user_terms: Iterable[object] | None = None,
) -> str:
    """返回空格分隔的词串，写入 FTS5 / 构造 MATCH 查询（query 与 doc 同口径）。"""
    language = _normalize_language(language)
    tokens = cut(text or "", language, user_terms=user_terms)
    if language.startswith("en"):
        tokens = [safe for token in tokens for safe in _english_fts_tokens(token)]
    return " ".join(tokens)


def tokenizer_version(
    language: str = "zh-CN",
    user_terms: Iterable[object] | None = None,
) -> str:
    """Return a language and dictionary-bound index implementation id."""
    language = _normalize_language(language)
    terms = _terms_for(language, user_terms)
    if language.startswith("en"):
        base = "unicode-latin-word-4-lemma"
    else:
        base = (
            f"jieba-{getattr(jieba, '__version__', 'none')}"
            if _HAS_JIEBA else "fallback-bigram-1"
        )
    dict_sha = hashlib.sha256("\x01".join(sorted(terms)).encode("utf-8")).hexdigest()[:12]
    return f"{language}:{base}+dict:{dict_sha}"
