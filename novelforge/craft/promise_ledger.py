"""F1 读者承诺账本（v22）：伏笔从"埋与收"升级为"埋—提醒—兑现"。

依据（docs/NovelForge/23 P0-1）：期待感 = 埋扣子 + 提醒 + 兑现（猫腻"大高潮前要挖好扣子"）；
Sanderson："读者弃书主因是进展路标不足"；MICE 线程后开先关；NstAgent 的待兑现义务。

扩展点（见 craft/extensions.py）：
- ``craft_checks``：休眠承诺、短线超时、开篇一长一短、契约承诺操作落空、大爽点前置扣子数、
  同章兑现过多（张力断崖）、嵌套关闭顺序（全部确定性、只读、warn）。
- ``draft_brief``：注入最久未触及的读者可见承诺（≤ ``inject_dormant_limit`` 条）与本章契约
  计划的承诺操作（带账本上下文），只进动态段。
- ``settle``：结算后确定性步骤——把契约 ``remind`` 落到账本（mention，幂等），并报告
  嵌套违规、本章兑现数、扣子不足的兑现与休眠承诺数。不调用 LLM。

所有阈值来自 ``cfg.promises``（:class:`~novelforge.config.PromiseConfig`）。
"""
from __future__ import annotations

import logging
import math
import re
import sqlite3
from dataclasses import asdict, fields
from typing import Any, Iterable, Mapping, Optional

from ..config import PromiseConfig
from ..ids import new_id
from ..planning.contract_fields import normalize_promise_actions

logger = logging.getLogger(__name__)

OPEN_STATES: tuple[str, ...] = ("planted", "reinforced", "misled", "overdue")
LINE_TYPES: tuple[str, ...] = ("long", "short", "micro")
VISIBILITIES: tuple[str, ...] = ("reader", "hidden")
PROMISE_KINDS: tuple[str, ...] = (
    "mice_milieu", "mice_inquiry", "mice_character", "mice_event",
    "clue", "vow", "grudge", "treasure", "info_gap", "relationship", "other",
)

# 章节字数缺失时的兜底（zh 按字、en 按词；与 PromiseConfig 的 short_line_max_* 口径一致）
ZH_UNITS_PER_CHAPTER = 3000
EN_UNITS_PER_CHAPTER = 2000

_ISSUE_CAP = 3                 # 每类逐条告警的上限（最严重的在前）
_BRIEF_ACTION_LIMIT = 6        # 承诺提醒块里最多列出的契约承诺操作
_LABEL_MATCH_THRESHOLD = 0.5   # 契约 label → 账本行的模糊映射阈值（同 foreshadow_settle._MAP_THRESHOLD）
_WORKSPACE_CFG_KEY = "promise_config"


# ── 配置与语言 ────────────────────────────────────────────────────────────────

def resolve_config(source: Any) -> PromiseConfig:
    """Accept a NovelForgeConfig, a PromiseConfig or a plain dict; default otherwise."""
    if isinstance(source, PromiseConfig):
        return source
    promises = getattr(source, "promises", None)
    if isinstance(promises, PromiseConfig):
        return promises
    if isinstance(source, Mapping):
        known = {f.name for f in fields(PromiseConfig)}
        try:
            return PromiseConfig(**{k: v for k, v in source.items() if k in known})
        except TypeError:
            pass
    return PromiseConfig()


def _is_english(language: Any) -> bool:
    return str(language or "").lower().replace("_", "-").startswith("en")


def _lang(language: Any) -> str:
    return "en" if _is_english(language) else "zh"


def default_units_per_chapter(language: Any) -> int:
    return EN_UNITS_PER_CHAPTER if _is_english(language) else ZH_UNITS_PER_CHAPTER


def short_line_limit(cfg: PromiseConfig, language: Any) -> int:
    """Short-line budget in the project's content unit (zh 字 / en words)."""
    if _is_english(language):
        return max(1, _int(cfg.short_line_max_words, 20_000) or 20_000)
    return max(1, _int(cfg.short_line_max_chars, 30_000) or 30_000)


# ── 行级纯函数 ────────────────────────────────────────────────────────────────

def _int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_promise_kind(raw: Any) -> Optional[str]:
    """Clean a free-text kind.  Known vocabulary is canonicalised
    (``"MICE-Inquiry"`` → ``"mice_inquiry"``); unknown kinds are kept as typed."""
    if raw is None:
        return None
    text = str(raw).strip()[:40]
    if not text:
        return None
    key = re.sub(r"[\s\-]+", "_", text.lower())
    return key if key in PROMISE_KINDS else text


def promise_kind_category(kind: Any) -> str:
    """Vocabulary bucket of a stored kind; unknown or empty → ``"other"``."""
    key = re.sub(r"[\s\-]+", "_", str(kind or "").strip().lower())
    return key if key in PROMISE_KINDS else "other"


def _with_defaults(row: dict) -> dict:
    """Fill v22 columns for rows read from pre-v22 databases or with bad values."""
    if row.get("line_type") not in LINE_TYPES:
        row["line_type"] = "long"
    if row.get("visibility") not in VISIBILITIES:
        row["visibility"] = "reader"
    row.setdefault("promise_kind", None)
    row.setdefault("parent_id", None)
    row.setdefault("remind_every", None)
    row.setdefault("importance", 3)
    row["label"] = str(row.get("label") or "")
    row["description"] = str(row.get("description") or "")
    return row


def effective_remind_every(row: Mapping[str, Any], cfg: PromiseConfig) -> int:
    """Explicit ``remind_every`` wins; otherwise long = default, short = max(2, default//2),
    micro = 1."""
    explicit = _int(row.get("remind_every"))
    if explicit is not None and explicit >= 1:
        return explicit
    default = max(1, _int(cfg.default_remind_every, 5) or 5)
    line_type = row.get("line_type") or "long"
    if line_type == "short":
        return max(2, default // 2)
    if line_type == "micro":
        return 1
    return default


def dormant_after(row: Mapping[str, Any], cfg: PromiseConfig) -> int:
    """Chapters without a touch after which a promise counts as dormant."""
    return effective_remind_every(row, cfg) * max(1, _int(cfg.dormant_factor, 2) or 2)


def last_touch_chapter(row: Mapping[str, Any]) -> int:
    """Latest of planted / last mentioned / last advanced chapter."""
    values = [
        _int(row.get(key))
        for key in ("planted_chapter", "last_mentioned_chapter", "last_advanced_chapter")
    ]
    present = [v for v in values if v is not None]
    return max(present) if present else 0


def chapters_since_touch(row: Mapping[str, Any], chapter: int) -> int:
    """Chapters elapsed since the last touch (0 when touched at/after ``chapter``)."""
    return max(0, int(chapter) - last_touch_chapter(row))


def is_open(row: Mapping[str, Any]) -> bool:
    return row.get("state") in OPEN_STATES


def is_remindable(row: Mapping[str, Any]) -> bool:
    """Open, reader-visible long/short line: the reader expects to hear about it."""
    return (
        is_open(row)
        and row.get("visibility", "reader") == "reader"
        and row.get("line_type", "long") in ("long", "short")
    )


def is_dormant(row: Mapping[str, Any], chapter: int, cfg: PromiseConfig) -> bool:
    return is_remindable(row) and chapters_since_touch(row, chapter) > dormant_after(row, cfg)


def is_due_for_reminder(row: Mapping[str, Any], chapter: int, cfg: PromiseConfig) -> bool:
    return (
        is_remindable(row)
        and chapters_since_touch(row, chapter) >= effective_remind_every(row, cfg)
    )


def dormancy_rank(row: Mapping[str, Any], chapter: int, cfg: PromiseConfig) -> tuple:
    """Sort key, most dormant first: silence relative to the promise's own cadence,
    then importance, raw silence, age and id (fully deterministic)."""
    gap = chapters_since_touch(row, chapter)
    every = effective_remind_every(row, cfg)
    return (
        -(gap / every),
        -(_int(row.get("importance"), 3) or 3),
        -gap,
        _int(row.get("planted_chapter"), 0) or 0,
        str(row.get("id") or ""),
    )


def annotate(row: dict, chapter: int, cfg: PromiseConfig) -> dict:
    """Add the derived reminder fields used by the API and reports."""
    row["last_touch_chapter"] = last_touch_chapter(row)
    row["chapters_since_touch"] = chapters_since_touch(row, chapter)
    row["effective_remind_every"] = effective_remind_every(row, cfg)
    row["dormant_after"] = dormant_after(row, cfg)
    return row


# ── 文本匹配（确定性，零 LLM）─────────────────────────────────────────────────

_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_NON_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)
_EN_WORD_RE = re.compile(r"[a-z0-9]+")
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;…\n]+|\.(?=\s|$)")
# 高频虚字/代词/量词：含这些字的二元组（的名、人身、他的…）在任意正文里都常见，
# 不能作为"提到了这条承诺"的证据——模糊匹配前先剔除。
_CJK_STOP_CHARS = frozenset(
    "的地得之了着过是在有和与及或而也都就才又还再已很不没未这那此其"
    "他她它我你您们个人把被给让对向从于以为所者吗呢吧啊呀么一会能要"
)
_EN_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "has",
    "had", "her", "his", "its", "in", "into", "is", "it", "of", "on", "or", "our",
    "the", "their", "them", "they", "this", "that", "to", "was", "were", "who",
    "whom", "whose", "will", "with", "what", "when", "where", "why", "how",
})


def _key(text: Any) -> str:
    """Casefolded text without whitespace/punctuation (CJK comparison key)."""
    return _NON_WORD_RE.sub("", str(text or "").casefold())


def _char_bigrams(key: str) -> set[str]:
    return {key[i:i + 2] for i in range(len(key) - 1)}


def _content_pieces(key: str) -> list[tuple[int, str]]:
    """``(position, bigram)`` of a CJK key, dropping bigrams that contain a
    function character (``_CJK_STOP_CHARS``)."""
    return [
        (i, key[i:i + 2]) for i in range(len(key) - 1)
        if key[i] not in _CJK_STOP_CHARS and key[i + 1] not in _CJK_STOP_CHARS
    ]


def _disjoint_count(positions: Iterable[int]) -> int:
    """Max number of non-overlapping bigrams among label ``positions``."""
    count, last = 0, -2
    for pos in sorted(set(positions)):
        if pos >= last + 2:
            count, last = count + 1, pos
    return count


def _en_words(text: Any) -> list[str]:
    return _EN_WORD_RE.findall(str(text or "").casefold().replace("’", "'").replace("'", ""))


def _content_tokens(text: Any) -> list[str]:
    seen: list[str] = []
    for word in _en_words(text):
        if len(word) >= 3 and word not in _EN_STOPWORDS and word not in seen:
            seen.append(word)
    return seen


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _snippet(text: str, limit: int = 80) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


class DraftMatcher:
    """Find promise references in one draft.

    Phrases tried: the label, plus the description when it is short enough to
    be a key phrase.  Exact match first (CJK: punctuation/space-insensitive
    substring; Latin: whole-phrase word match), then an optional fuzzy
    fallback inside one sentence:

    - CJK phrases of ≥4 characters: only *content* bigrams count (bigrams
      containing a function character such as 的/人/他 are dropped); within a
      window of about the phrase's length (+4) the sentence must contain ≥50%
      of them **and** at least two non-overlapping ones.  So 「灭门真凶」 matches
      「那灭门的真凶」, while 「仇人的名字」 does not match 「他的名字叫张三」
      and 「黑袍人身份」 does not match 「此人身份不明」.
    - Latin phrases: ≥50% (and at least two) of their content words.

    Misses are the safe failure (an extra dormant warning); false hits would
    silence checks, so the fuzzy rule is deliberately conservative.  Built once
    per draft and reused.
    """

    def __init__(self, text: str):
        self.text = str(text or "")
        self._key = _key(self.text)
        self._words = " " + " ".join(_en_words(self.text)) + " "
        self._sentences = [
            s.strip() for s in _SENTENCE_SPLIT_RE.split(self.text) if s and s.strip()
        ]
        self._sentence_keys: Optional[list[str]] = None
        self._sentence_bigrams: Optional[list[set[str]]] = None
        self._sentence_stems: Optional[list[set[str]]] = None

    # -- lazy per-sentence indexes -------------------------------------------------
    def _keys(self) -> list[str]:
        if self._sentence_keys is None:
            self._sentence_keys = [_key(s) for s in self._sentences]
        return self._sentence_keys

    def _bigram_index(self) -> list[set[str]]:
        if self._sentence_bigrams is None:
            self._sentence_bigrams = [_char_bigrams(k) for k in self._keys()]
        return self._sentence_bigrams

    def _stem_index(self) -> list[set[str]]:
        if self._sentence_stems is None:
            self._sentence_stems = [
                {_stem(w) for w in _en_words(s)} for s in self._sentences
            ]
        return self._sentence_stems

    def _sentence_with_key(self, key: str) -> str:
        for sentence, sentence_key in zip(self._sentences, self._keys()):
            if key in sentence_key:
                return sentence
        return ""

    # -- matching ----------------------------------------------------------------
    def _exact(self, phrase: str) -> Optional[str]:
        if _CJK_RE.search(phrase):
            key = _key(phrase)
            if len(key) >= 2 and key in self._key:
                return _snippet(self._sentence_with_key(key) or phrase)
            return None
        words = _en_words(phrase)
        if not words or (len(words) == 1 and len(words[0]) < 3):
            return None
        joined = " " + " ".join(words) + " "
        if joined not in self._words:
            return None
        key = "".join(words)
        return _snippet(self._sentence_with_key(key) or phrase)

    def _fuzzy_cjk(self, label: str) -> Optional[str]:
        key = _key(label)
        pieces = _content_pieces(key)
        distinct = {gram for _, gram in pieces}
        if len(key) < 4 or len(distinct) < 2:
            return None
        window = len(key) + max(4, len(key) // 2)
        for sentence, sentence_grams, sentence_key in zip(
                self._sentences, self._bigram_index(), self._keys()):
            if len(distinct & sentence_grams) < 2:
                continue
            # (sentence position, label position, bigram) of every occurrence
            occurrences = sorted(
                (start, pos, gram)
                for pos, gram in pieces if gram in sentence_grams
                for start in _find_all(sentence_key, gram)
            )
            for i, (first, _, _) in enumerate(occurrences):
                inside = [(pos, gram) for start, pos, gram in occurrences[i:]
                          if start + 2 - first <= window]
                grams = {gram for _, gram in inside}
                if (len(grams) * 2 >= len(distinct)
                        and _disjoint_count(pos for pos, _ in inside) >= 2):
                    return _snippet(sentence)
        return None

    def _fuzzy(self, label: str) -> Optional[str]:
        if _CJK_RE.search(label):
            return self._fuzzy_cjk(label)
        tokens = [_stem(t) for t in _content_tokens(label)]
        if len(tokens) < 2:
            return None
        need = max(2, math.ceil(len(tokens) * 0.5))
        for sentence, stems in zip(self._sentences, self._stem_index()):
            if sum(1 for t in tokens if t in stems) >= need:
                return _snippet(sentence)
        return None

    def find(self, label: Any, description: Any = None, *,
             fuzzy: bool = True) -> Optional[str]:
        """Return an evidence snippet when the promise is referenced, else ``None``.

        ``fuzzy=False`` accepts exact phrase hits only (used before writing
        the ledger)."""
        phrases = [p for p in (str(label or "").strip(),) if p]
        description = str(description or "").strip()
        if description and _is_key_phrase(description):
            phrases.append(description)
        if not self.text.strip() or not phrases:
            return None
        for phrase in phrases:
            hit = self._exact(phrase)
            if hit:
                return hit
        if not fuzzy:
            return None
        for phrase in phrases:
            hit = self._fuzzy(phrase)
            if hit:
                return hit
        return None

    def mentions(self, label: Any, description: Any = None, *, fuzzy: bool = True) -> bool:
        return self.find(label, description, fuzzy=fuzzy) is not None


def _find_all(text: str, needle: str) -> Iterable[int]:
    start = text.find(needle)
    while start != -1:
        yield start
        start = text.find(needle, start + 1)


def _is_key_phrase(text: str) -> bool:
    if _CJK_RE.search(text):
        return 3 <= len(_key(text)) <= 12
    return 2 <= len(_en_words(text)) <= 6


def label_in_text(label: Any, text: str, *, description: Any = None) -> bool:
    """Convenience wrapper around :class:`DraftMatcher` for one-off checks."""
    return DraftMatcher(text).mentions(label, description)


def _row_found(matcher: DraftMatcher, row: Optional[Mapping[str, Any]],
               extra_label: Any = None, *, fuzzy: bool = True) -> Optional[str]:
    if extra_label:
        hit = matcher.find(extra_label, fuzzy=fuzzy)
        if hit:
            return hit
    if row is not None:
        return matcher.find(row.get("label"), row.get("description"), fuzzy=fuzzy)
    return None


def _label_similarity(a: Any, b: Any) -> float:
    if _CJK_RE.search(str(a or "")) or _CJK_RE.search(str(b or "")):
        ga, gb = _char_bigrams(_key(a)), _char_bigrams(_key(b))
    else:
        ga, gb = set(map(_stem, _content_tokens(a))), set(map(_stem, _content_tokens(b)))
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


# ── 数据库读取（全部容错：老库/缺表 → 空）──────────────────────────────────────

def _dicts(cursor: sqlite3.Cursor) -> list[dict]:
    names = [d[0] for d in cursor.description or ()]
    return [dict(zip(names, tuple(row))) for row in cursor.fetchall()]


def fetch_promises(
    conn: sqlite3.Connection, *, as_of_chapter: Optional[int] = None, open_only: bool = True,
) -> list[dict]:
    """Promise rows (all columns, v22 defaults filled) ordered by planted chapter.

    ``as_of_chapter`` drops rows planted after that chapter (regenerating an
    early chapter must not see later promises)."""
    sql = "SELECT * FROM foreshadow"
    clauses: list[str] = []
    params: list[Any] = []
    if open_only:
        clauses.append(f"state IN ({','.join('?' * len(OPEN_STATES))})")
        params.extend(OPEN_STATES)
    if as_of_chapter is not None:
        clauses.append("planted_chapter<=?")
        params.append(int(as_of_chapter))
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY planted_chapter, id"
    try:
        return [_with_defaults(r) for r in _dicts(conn.execute(sql, params))]
    except sqlite3.Error:
        return []


def fetch_open_promises(
    conn: sqlite3.Connection, *, as_of_chapter: Optional[int] = None,
) -> list[dict]:
    return fetch_promises(conn, as_of_chapter=as_of_chapter, open_only=True)


def dormant_promises(
    conn: sqlite3.Connection, chapter: int, cfg: Any = None,
) -> list[dict]:
    """Dormant reader-visible long/short promises as of ``chapter``, most dormant
    first, annotated with ``chapters_since_touch`` and friends."""
    pcfg = resolve_config(cfg)
    rows = [r for r in fetch_open_promises(conn, as_of_chapter=chapter)
            if is_dormant(r, chapter, pcfg)]
    rows.sort(key=lambda r: dormancy_rank(r, chapter, pcfg))
    return [annotate(r, chapter, pcfg) for r in rows]


def content_units_between(
    conn: Optional[sqlite3.Connection], start: int, end: int, *, language: Any = "zh-CN",
) -> int:
    """Content units (zh 字 / en words) of chapters ``start <= c < end``.

    Prefers ``chapter_cards.word_count`` of drafted chapters, then the latest
    ``draft_index`` revision, then the per-language default per chapter."""
    start, end = int(start), int(end)
    if end <= start:
        return 0
    per_chapter = default_units_per_chapter(language)
    cards: dict[int, int] = {}
    drafts: dict[int, int] = {}
    if conn is not None:
        try:
            for ch, wc in conn.execute(
                "SELECT chapter, word_count FROM chapter_cards"
                " WHERE chapter>=? AND chapter<? AND word_count>0", (start, end),
            ).fetchall():
                cards[int(ch)] = int(wc)
            for ch, wc in conn.execute(
                "SELECT chapter, word_count FROM draft_index"
                " WHERE chapter>=? AND chapter<? AND word_count>0"
                " ORDER BY chapter, revision_round", (start, end),
            ).fetchall():
                drafts[int(ch)] = int(wc)   # 升序遍历：最终留下最新修订
        except sqlite3.Error:
            pass
    return sum(cards.get(c, drafts.get(c, per_chapter)) for c in range(start, end))


def touch_count(conn: Optional[sqlite3.Connection], row: Mapping[str, Any],
                before_chapter: int) -> int:
    """Setup depth before ``before_chapter``: 1 for planting + distinct later
    chapters with a mention/advance (``foreshadow_log`` ∪ last_* columns)."""
    planted = _int(row.get("planted_chapter"), 0) or 0
    chapters: set[int] = set()
    if conn is not None and row.get("id"):
        try:
            for (ch,) in conn.execute(
                "SELECT DISTINCT chapter FROM foreshadow_log"
                " WHERE foreshadow_id=? AND action IN ('mention','advance') AND chapter<?",
                (row["id"], int(before_chapter)),
            ).fetchall():
                chapters.add(int(ch))
        except sqlite3.Error:
            pass
    for key in ("last_mentioned_chapter", "last_advanced_chapter"):
        value = _int(row.get(key))
        if value is not None and value < before_chapter:
            chapters.add(value)
    return 1 + sum(1 for c in chapters if c > planted)


def is_unconfirmed_settle_hook(row: Mapping[str, Any]) -> bool:
    """A hook the settler created on its own (``origin='settle'``) that nobody has
    curated since: still the creation defaults (importance ≤ 2, no ``remind_every``).

    Its ``line_type='short'`` is only a guess — many such hooks are really long
    threads the settler keeps mentioning/advancing — and nothing in the pipeline
    can retag it, so the short-line budget would nag about it every chapter
    forever.  Such rows are left to the dormancy check (which a mention clears);
    raising the importance or setting ``remind_every`` confirms the row."""
    return (
        str(row.get("origin") or "") == "settle"
        and (_int(row.get("importance"), 2) or 0) <= 2
        and _int(row.get("remind_every")) is None
    )


def overdue_short_lines(
    conn: Optional[sqlite3.Connection], rows: Iterable[Mapping[str, Any]], chapter: int,
    cfg: PromiseConfig, language: Any, *, skip_ids: Iterable[str] = (),
) -> list[tuple[int, dict]]:
    """Open reader-visible short lines whose content since planting (chapters
    ``planted <= c < chapter``) exceeds the short-line budget, as
    ``[(units, row)]`` most over budget first.  Unconfirmed settle hooks are
    skipped (see :func:`is_unconfirmed_settle_hook`)."""
    limit = short_line_limit(cfg, language)
    skip = set(skip_ids)
    out: list[tuple[int, dict]] = []
    for row in rows:
        if (row["id"] in skip or not is_open(row) or row.get("line_type") != "short"
                or row.get("visibility") != "reader" or is_unconfirmed_settle_hook(row)):
            continue
        planted = _int(row.get("planted_chapter"), chapter) or chapter
        units = content_units_between(conn, planted, chapter, language=language)
        if units > limit:
            out.append((units, dict(row)))
    out.sort(key=lambda item: (-item[0], str(item[1]["id"])))
    return out


def _open_children(conn: Optional[sqlite3.Connection], parent_id: str,
                   *, exclude: Iterable[str] = ()) -> list[dict]:
    if conn is None or not parent_id:
        return []
    try:
        rows = _dicts(conn.execute(
            "SELECT id, label FROM foreshadow WHERE parent_id=?"
            f" AND state IN ({','.join('?' * len(OPEN_STATES))}) ORDER BY planted_chapter, id",
            (parent_id, *OPEN_STATES),
        ))
    except sqlite3.Error:
        return []
    skip = set(exclude)
    return [r for r in rows if r["id"] not in skip]


def _ledger_has_rows(conn: Optional[sqlite3.Connection]) -> bool:
    if conn is None:
        return False
    try:
        return conn.execute("SELECT 1 FROM foreshadow LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


# ── 契约承诺操作 ──────────────────────────────────────────────────────────────

def contract_promise_actions(workspace: Any, chapter: int) -> list[dict]:
    """Normalised ``promise_actions`` of this chapter's contract (``[]`` if absent)."""
    if not isinstance(workspace, Mapping):
        return []
    plan_context = workspace.get("plan_context")
    if plan_context is None:
        return []
    if isinstance(plan_context, Mapping):
        target = plan_context.get("target_chapter", chapter)
        contract = plan_context.get("current_contract")
    else:
        target = getattr(plan_context, "target_chapter", chapter)
        contract = getattr(plan_context, "current_contract", None)
    if _int(target, chapter) != int(chapter) or not isinstance(contract, Mapping):
        return []
    return normalize_promise_actions(contract.get("promise_actions"))


def _volume_end(workspace: Any) -> Optional[int]:
    if not isinstance(workspace, Mapping):
        return None
    plan_context = workspace.get("plan_context")
    volume = (plan_context.get("current_volume") if isinstance(plan_context, Mapping)
              else getattr(plan_context, "current_volume", None))
    return _int(volume.get("end_chapter")) if isinstance(volume, Mapping) else None


def resolve_action(action: Mapping[str, Any], rows: list[dict]) -> Optional[dict]:
    """Map a contract action onto a ledger row: id, then exact label, then the
    best bigram/word match ≥ 0.5.  Open rows win ties."""
    by_id = {r["id"]: r for r in rows}
    promise_id = action.get("promise_id")
    if promise_id and promise_id in by_id:
        return by_id[promise_id]
    label = action.get("label") or ""
    if not label:
        return None
    ordered = sorted(rows, key=lambda r: (not is_open(r),))
    key = _key(label)
    for row in ordered:
        if key and _key(row["label"]) == key:
            return row
    best, best_sim = None, 0.0
    for row in ordered:
        sim = _label_similarity(label, row["label"])
        if sim > best_sim:
            best, best_sim = row, sim
    if best_sim >= _LABEL_MATCH_THRESHOLD:
        return best
    # 中文包含关系兜底：「血仇未报」↔ 账本「血仇」（二元组 Jaccard 只有 1/3）。
    # 较短一方 ≥2 字且至少占较长一方的一半，避免「师父」吞掉「师父的遗言」这类另一条承诺。
    if _CJK_RE.search(label):
        contained = [r for r in ordered if _contains_label(key, _key(r["label"]))]
        if contained:
            return max(contained, key=lambda r: (is_open(r), len(_key(r["label"]))))
    return None


def _contains_label(a: str, b: str) -> bool:
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 2 and short in long_ and len(short) * 2 >= len(long_)


# ── 文案（zh/en）──────────────────────────────────────────────────────────────

_LINE_LABELS = {
    "zh": {"long": "长线", "short": "短线", "micro": "微线"},
    "en": {"long": "long line", "short": "short line", "micro": "micro line"},
}
_ACTION_VERBS = {
    "zh": {"plant": "埋设", "remind": "提醒", "advance": "推进", "payoff": "兑现"},
    "en": {"plant": "plant", "remind": "remind", "advance": "advance", "payoff": "pay off"},
}
_ACTION_NOUNS = {   # 提醒块里的名词形式（中文与动词同形）
    "zh": _ACTION_VERBS["zh"],
    "en": {"plant": "plant", "remind": "reminder", "advance": "advance", "payoff": "payoff"},
}
_TEXT = {
    "zh": {
        "dormant": "读者承诺「{label}」（{line}，第{planted}章埋下）已 {gap} 章未被提及或推进"
                   "（提醒周期 {every} 章），读者可能已经忘了它",
        "dormant_fix": "在本章自然呼应/提醒「{label}」：让角色想起、线索浮现或相关人物出场，不要生硬复述",
        "short": "短线承诺「{label}」第{planted}章埋下，至今约 {units} 字仍未收束"
                 "（短线建议 {limit} 字内揭开），本章也没有推进或兑现它",
        "short_fix": "本章推进或揭开「{label}」；若它其实是长线，请在账本中改为 long",
        "opening": "开篇第{chapter}章结束前应至少埋下 1 条长线 + 1 条短线读者可见承诺"
                   "（一长一短），目前缺少：{missing}",
        "opening_fix": "在本章埋下缺少的{missing}承诺（短线：几章内就会揭晓的悬念/约定/威胁；"
                       "长线：贯穿全卷的大目标或大谜团），并在账本里标注 line_type",
        "opening_one": "开篇第{chapter}章结束前应至少埋下 1 条长线 + 1 条短线读者可见承诺"
                       "（一长一短）；账本里两类都还没有，本章计划的新埋设只能补上其中一类",
        "opening_one_fix": "除计划中的埋设外再埋下 1 条承诺，保证长线、短线各有一条"
                           "（短线：几章内就会揭晓的悬念/约定/威胁；长线：贯穿全卷的大目标或大谜团），"
                           "并在账本里标注 line_type",
        "missing": "本章契约计划{verb}承诺「{label}」，但正文里找不到相关内容",
        "missing_fix": "在本章{verb}「{label}」{note}",
        "thin": "本章计划兑现「{label}」，但它自第{planted}章埋下后只被触及 {touches} 次"
                "（埋设算 1 次；大爽点前建议至少触及 {need} 次）——扣子没挖够，兑现会显得突兀",
        "thin_fix": "兑现时回扣此前埋下的细节，让兑现有来处；铺垫按此前章节的提醒/推进计数，"
                    "本章内补的提醒不算——若扣子确实不够，把本章的兑现改为推进（advance），"
                    "后续章节再提醒/推进 {more} 次后兑现",
        "cliff": "本章计划同时兑现 {count} 条承诺（{labels}），超过每章 {limit} 条——"
                 "集中兑现会造成张力断崖，后续章节失去期待",
        "cliff_fix": "把部分兑现拆到后续章节，或把其中一条改为推进（advance）",
        "nesting": "本章计划兑现外层承诺「{label}」，但内层承诺{children}仍未收束（后开先关：内层先收）",
        "nesting_fix": "先收束内层承诺{children}，再兑现「{label}」；或调整承诺的嵌套关系",
        "and": "、",
        "quote": "「{}」",
        "brief_header": "## 承诺提醒（本章需自然呼应，不要生硬复述）",
        "brief_due": "- {label}（第{planted}章埋下，已 {gap} 章未提及）",
        "brief_action": "- 本章计划{verb}：{label}",
        "brief_planted": "（第{planted}章埋下）",
        "brief_planned_suffix": "；本章计划{verb}",
        "brief_children": "；先收束内层承诺{children}",
        "brief_thin": "；铺垫尚浅（仅触及 {touches} 次）：兑现时回扣此前埋下的细节，让兑现有来处",
        "brief_overdue": "- {label}（短线，第{planted}章埋下，约 {units} 字仍未收束，"
                         "超出 {limit} 字预算）",
        "brief_overdue_tail": "；本章推进或揭开它",
        "brief_overdue_suffix": "；短线已超出 {limit} 字预算（约 {units} 字），本章推进或揭开它",
        "colon": "：",
        "long": "长线", "short_line": "短线",
    },
    "en": {
        "dormant": 'Reader promise "{label}" ({line}, planted in chapter {planted}) has gone '
                   "{gap} chapters without a mention or advance (reminder cadence: every "
                   "{every} chapters); readers may have forgotten it.",
        "dormant_fix": 'Weave a natural reminder of "{label}" into this chapter (a thought, a '
                       "surfacing clue, or a related character) without recapping it.",
        "short": 'Short-line promise "{label}" was planted in chapter {planted} and is still '
                 "open after about {units} words (short lines should close within {limit}); "
                 "this chapter neither advances nor pays it off.",
        "short_fix": 'Advance or resolve "{label}" in this chapter; if it is really a long '
                     "line, retag it as long in the promise ledger.",
        "opening": "By the end of chapter {chapter} the opening should have planted at least "
                   "one long-line and one short-line reader promise; missing: {missing}.",
        "opening_fix": "Plant the missing {missing} promise in this chapter (short: a question "
                       "answered within a few chapters; long: a goal or mystery spanning the "
                       "volume) and tag its line_type in the ledger.",
        "opening_one": "By the end of chapter {chapter} the opening should have planted at least "
                       "one long-line and one short-line reader promise; the ledger has neither "
                       "yet, and the plant planned for this chapter can cover only one of them.",
        "opening_one_fix": "Besides the planned plant, plant one more promise so there is one "
                           "long line (a goal or mystery spanning the volume) and one short line "
                           "(a question answered within a few chapters), and tag their line_type "
                           "in the ledger.",
        "missing": 'The chapter contract plans to {verb} promise "{label}", but the draft '
                   "never touches it.",
        "missing_fix": '{verb_cap} "{label}" in this chapter{note}.',
        "thin": 'This chapter plans to pay off "{label}", but since it was planted in '
                "chapter {planted} it has been touched only {touches} time(s), counting the "
                "plant (a big payoff wants at least {need} touches); the setup is too thin and "
                "the payoff may feel unearned.",
        "thin_fix": "Call back to the details planted earlier so the payoff feels earned. Setup "
                    "is counted from earlier chapters, so a reminder added in this chapter does "
                    "not count; if the setup really is too thin, turn this chapter's payoff into "
                    "an advance and pay it off after {more} more reminder(s) or advance(s) in "
                    "later chapters.",
        "cliff": "This chapter plans to pay off {count} promises at once ({labels}), more than "
                 "{limit} per chapter; bunching payoffs causes a tension cliff afterwards.",
        "cliff_fix": "Move some payoffs to later chapters, or turn one of them into an advance.",
        "nesting": 'This chapter plans to pay off the outer promise "{label}" while inner '
                   "promise(s) {children} are still open (first in, last out).",
        "nesting_fix": 'Close the inner promise(s) {children} before paying off "{label}", or '
                       "restructure the nesting.",
        "and": ", ",
        "quote": '"{}"',
        "brief_header": "## Promise reminders (weave in naturally, do not recap)",
        "brief_due": "- {label} (planted in chapter {planted}, untouched for {gap} chapters)",
        "brief_action": "- Planned {verb}: {label}",
        "brief_planted": " (planted in chapter {planted})",
        "brief_planned_suffix": "; planned this chapter: {verb}",
        "brief_children": "; close the inner promise(s) {children} first",
        "brief_thin": "; setup is thin (touched {touches} time(s)): call back to the details "
                      "planted earlier so the payoff feels earned",
        "brief_overdue": "- {label} (short line, planted in chapter {planted}, still open after "
                         "about {units} words, over the {limit}-word budget)",
        "brief_overdue_tail": "; advance or resolve it this chapter",
        "brief_overdue_suffix": "; short line over its {limit}-word budget (about {units} words),"
                                " advance or resolve it this chapter",
        "colon": ": ",
        "long": "long line", "short_line": "short line",
    },
}


def _clip(text: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _quote_list(lang: str, labels: Iterable[str]) -> str:
    t = _TEXT[lang]
    return t["and"].join(t["quote"].format(_clip(label, 40)) for label in labels)


def _issue(check: str, detail: str, fix: str) -> dict:
    """Craft issue dict (extensions.normalize_issue shape); ledger findings are
    about absence, so they carry no draft span."""
    return {
        "check": check, "severity": "warn", "detail": detail, "fix": fix,
        "repair_scope": "local", "category": f"craft.{check}",
    }


# ── craft_checks ─────────────────────────────────────────────────────────────

def _ctx_language(ctx: Any) -> str:
    workspace = getattr(ctx, "workspace", None) or {}
    extra = getattr(ctx, "extra", None) or {}
    for profile in (workspace.get("writing_profile"), extra.get("writing_profile")):
        if isinstance(profile, Mapping):
            language = profile.get("content_language") or profile.get("language")
            if language:
                return str(language)
    conn = getattr(ctx, "conn", None)
    if conn is not None:
        try:
            from ..writing_profiles import get_project_writing_profile
            profile = get_project_writing_profile(
                conn, chapter=getattr(ctx, "target_chapter", None),
            )
            return str(profile.get("content_language") or "zh-CN")
        except Exception:   # 读取 profile 失败不应让检查报错；按中文处理
            logger.debug("promise ledger: writing profile unavailable", exc_info=True)
    return "zh-CN"


def craft_checks(ctx: Any) -> list[dict]:
    """Deterministic promise-ledger warnings for the chapter being checked.

    Silent when the ledger is disabled, the draft is empty, or there are no
    relevant ledger rows / contract promise actions."""
    workspace = getattr(ctx, "workspace", None)
    conn = getattr(ctx, "conn", None)
    chapter = _int(getattr(ctx, "target_chapter", None), 0) or 0
    if not isinstance(workspace, Mapping) or conn is None or chapter < 1:
        return []
    extra = getattr(ctx, "extra", None) or {}
    cfg = resolve_config(workspace.get(_WORKSPACE_CFG_KEY) or extra.get(_WORKSPACE_CFG_KEY))
    draft = str(workspace.get("draft_text") or "")
    if not cfg.enabled or not draft.strip():
        return []

    open_rows = fetch_open_promises(conn, as_of_chapter=chapter)
    actions = contract_promise_actions(workspace, chapter)
    opening = chapter == cfg.opening_window_chapters
    if not open_rows and not actions and not (opening and _ledger_has_rows(conn)):
        return []
    language = _ctx_language(ctx)
    lang = _lang(language)
    matcher = DraftMatcher(draft)
    all_rows = fetch_promises(conn, as_of_chapter=chapter, open_only=False) if actions else []
    resolved = [(a, resolve_action(a, all_rows)) for a in actions]
    # 契约已安排触及的承诺交给契约检查，不再重复报休眠/超时
    planned_ids = {
        row["id"] for action, row in resolved
        if row is not None and action["action"] in ("remind", "advance", "payoff")
    }

    overdue = _check_short_overdue(conn, open_rows, chapter, cfg, matcher, language,
                                   skip_ids=planned_ids)
    issues = _check_dormant(open_rows, chapter, cfg, matcher, lang,
                            skip_ids=planned_ids | {row_id for row_id, _ in overdue})
    issues += [issue for _, issue in overdue]
    if opening:
        issues += _check_opening(conn, chapter, resolved, lang)
    if resolved:
        issues += _check_contract(conn, chapter, cfg, resolved, matcher, lang, workspace)
    return issues


def _check_dormant(rows: list[dict], chapter: int, cfg: PromiseConfig,
                   matcher: DraftMatcher, lang: str, *, skip_ids: set[str]) -> list[dict]:
    """休眠：读者可见的长/短线超过 remind_every × dormant_factor 章未触及，且本章正文也没提。"""
    t = _TEXT[lang]
    dormant = [r for r in rows if r["id"] not in skip_ids and is_dormant(r, chapter, cfg)]
    dormant.sort(key=lambda r: dormancy_rank(r, chapter, cfg))
    out: list[dict] = []
    for row in dormant:
        if len(out) >= _ISSUE_CAP:
            break
        if matcher.mentions(row["label"], row["description"]):
            continue
        label = _clip(row["label"], 40)
        out.append(_issue(
            "promise_dormant",
            t["dormant"].format(
                label=label, line=_LINE_LABELS[lang][row["line_type"]],
                planted=row["planted_chapter"], gap=chapters_since_touch(row, chapter),
                every=effective_remind_every(row, cfg),
            ),
            t["dormant_fix"].format(label=label),
        ))
    return out


def _check_short_overdue(conn, rows: list[dict], chapter: int, cfg: PromiseConfig,
                         matcher: DraftMatcher, language: str, *,
                         skip_ids: set[str]) -> list[tuple[str, dict]]:
    """短线超时：读者可见短线自埋下起（不含本章）累计字数超过上限，且本章正文未触及。

    Returns ``[(row_id, issue)]`` so the caller can suppress a duplicate dormant warn."""
    t = _TEXT[_lang(language)]
    limit = short_line_limit(cfg, language)
    out: list[tuple[str, dict]] = []
    for units, row in overdue_short_lines(conn, rows, chapter, cfg, language,
                                          skip_ids=skip_ids):
        if len(out) >= _ISSUE_CAP:
            break
        if matcher.mentions(row["label"], row["description"]):
            continue
        label = _clip(row["label"], 40)
        out.append((row["id"], _issue(
            "promise_short_overdue",
            t["short"].format(label=label, planted=row["planted_chapter"],
                              units=f"{units:,}", limit=f"{limit:,}"),
            t["short_fix"].format(label=label),
        )))
    return out


def _check_opening(conn, chapter: int, resolved: list[tuple[dict, Optional[dict]]],
                   lang: str) -> list[dict]:
    """开篇一长一短：读者可见的长线/短线承诺各至少 1 条（任何状态都算"已承诺"）。"""
    if not _ledger_has_rows(conn) and not resolved:
        return []
    rows = fetch_promises(conn, as_of_chapter=chapter, open_only=False)
    present = {r["line_type"] for r in rows if r["visibility"] == "reader"}
    wildcards = 0
    for action, row in resolved:
        if action["action"] != "plant":
            continue
        if row is None:
            wildcards += 1        # 未建档的计划埋设：类型未知，宽容地补一个空缺
        elif row["visibility"] == "reader":
            present.add(row["line_type"])
    missing = [lt for lt in ("long", "short") if lt not in present]
    if len(missing) <= wildcards:
        return []
    t = _TEXT[lang]
    if wildcards:
        # 两类都缺、本章只计划埋 1 条未建档承诺：它能补哪一类未知，只提示"还差一条"
        return [_issue(
            "promise_opening",
            t["opening_one"].format(chapter=chapter),
            t["opening_one_fix"],
        )]
    names = t["and"].join(t["long"] if lt == "long" else t["short_line"] for lt in missing)
    return [_issue(
        "promise_opening",
        t["opening"].format(chapter=chapter, missing=names),
        t["opening_fix"].format(missing=names),
    )]


def _check_contract(conn, chapter: int, cfg: PromiseConfig,
                    resolved: list[tuple[dict, Optional[dict]]], matcher: DraftMatcher,
                    lang: str, workspace: Mapping[str, Any]) -> list[dict]:
    t = _TEXT[lang]
    verbs = _ACTION_VERBS[lang]
    out: list[dict] = []

    # a) 计划的提醒/推进/兑现在正文里落空
    seen: set[str] = set()
    for action, row in resolved:
        if action["action"] not in ("remind", "advance", "payoff"):
            continue
        if row is None and not action.get("label"):
            continue     # 只给了未知 promise_id：无从判断，不误报
        name = _clip((row or {}).get("label") or action.get("label"), 40)
        ident = (row or {}).get("id") or _key(name)
        if ident in seen:
            continue
        seen.add(ident)
        if _row_found(matcher, row, action.get("label")):
            continue
        verb = verbs[action["action"]]
        note = action.get("note")
        note_text = (f"（{_clip(note, 60)}）" if lang == "zh" else f" ({_clip(note, 80)})") \
            if note else ""
        out.append(_issue(
            "promise_contract_missing",
            t["missing"].format(verb=verb, label=name),
            t["missing_fix"].format(verb=verb, verb_cap=verb[:1].upper() + verb[1:],
                                    label=name, note=note_text),
        ))

    payoffs: list[tuple[dict, Optional[dict]]] = []
    payoff_keys: set[str] = set()
    for action, row in resolved:
        if action["action"] != "payoff":
            continue
        key = (row or {}).get("id") or _key(action.get("label") or action.get("promise_id"))
        if key and key not in payoff_keys:
            payoff_keys.add(key)
            payoffs.append((action, row))
    payoff_row_ids = {row["id"] for _, row in payoffs if row is not None}

    # b) 大爽点前置：扣子没挖够
    need = max(1, _int(cfg.big_payoff_min_touches, 2) or 2)
    for _, row in payoffs:
        if row is None or not is_open(row) or not _is_big(row):
            continue
        touches = touch_count(conn, row, chapter)
        if touches < need:
            label = _clip(row["label"], 40)
            out.append(_issue(
                "promise_thin_setup",
                t["thin"].format(label=label, planted=row["planted_chapter"],
                                 touches=touches, need=need),
                t["thin_fix"].format(more=need - touches),
            ))

    # c) 张力断崖：同章兑现过多（卷末章除外）
    limit = max(1, _int(cfg.max_payoffs_per_chapter, 2) or 2)
    if len(payoffs) > limit and _volume_end(workspace) != chapter:
        labels = [(row or {}).get("label") or action.get("label") or action.get("promise_id")
                  for action, row in payoffs]
        out.append(_issue(
            "promise_payoff_cliff",
            t["cliff"].format(count=len(payoffs), labels=_quote_list(lang, labels), limit=limit),
            t["cliff_fix"],
        ))

    # d) 嵌套关闭顺序：外层兑现时内层仍开着（同章一并兑现的内层不算）
    for _, row in payoffs:
        if row is None:
            continue
        children = _open_children(conn, row["id"], exclude=payoff_row_ids)
        if children:
            label = _clip(row["label"], 40)
            child_text = _quote_list(lang, [c["label"] for c in children[:3]])
            out.append(_issue(
                "promise_nesting",
                t["nesting"].format(label=label, children=child_text),
                t["nesting_fix"].format(label=label, children=child_text),
            ))
    return out


def _is_big(row: Mapping[str, Any]) -> bool:
    return (_int(row.get("importance"), 3) or 3) >= 4 or row.get("line_type") == "long"


# ── draft_brief ──────────────────────────────────────────────────────────────

def draft_brief(conn: Optional[sqlite3.Connection], chapter: int, workspace: Any, *,
                language: str, cfg: Any) -> str:
    """Dynamic "promise reminders" block for the planner and the drafter.

    Lists up to ``inject_dormant_limit`` reader-visible open promises that are
    due for a reminder (most dormant first; raised to the dormant promises the
    ``promise_dormant`` check would warn about, at most ``_ISSUE_CAP``), up to
    ``inject_dormant_limit`` short lines that are
    over their character/word budget (the ``promise_short_overdue`` check's
    rule), plus the contract's planned promise actions not already listed, each
    with ledger context.  Returns "" when nothing applies.  Also records the
    effective config in the workspace so the craft checks of this chapter use
    the same thresholds."""
    pcfg = resolve_config(cfg)
    if isinstance(workspace, dict):
        workspace[_WORKSPACE_CFG_KEY] = asdict(pcfg)
    chapter = _int(chapter, 0) or 0
    if conn is None or not pcfg.enabled or chapter < 1:
        return ""
    lang = _lang(language)
    t = _TEXT[lang]
    nouns = _ACTION_NOUNS[lang]
    desc_limit = 60 if lang == "zh" else 140

    open_rows = fetch_open_promises(conn, as_of_chapter=chapter)
    actions = contract_promise_actions(workspace, chapter)
    if not open_rows and not actions:
        return ""
    all_rows = fetch_promises(conn, as_of_chapter=chapter, open_only=False) if actions else []
    resolved = [(a, resolve_action(a, all_rows)) for a in actions]
    planned_verb: dict[str, str] = {}
    for action, row in resolved:
        if row is not None:
            planned_verb.setdefault(row["id"], action["action"])

    payoff_ids = {row["id"] for action, row in resolved
                  if row is not None and action["action"] == "payoff"}
    need = max(1, _int(pcfg.big_payoff_min_touches, 2) or 2)

    def payoff_hints(row: dict) -> str:
        if row["id"] not in payoff_ids:
            return ""
        hints = ""
        children = _open_children(conn, row["id"], exclude=payoff_ids)
        if children:
            hints += t["brief_children"].format(
                children=_quote_list(lang, [c["label"] for c in children[:3]]))
        if _is_big(row):
            touches = touch_count(conn, row, chapter)
            if touches < need:
                hints += t["brief_thin"].format(touches=touches)
        return hints

    limit = max(0, _int(pcfg.inject_dormant_limit, 2) or 0)
    # craft check promise_dormant 最多逐条报 _ISSUE_CAP 条休眠承诺：休眠的比注入上限多时，
    # 把会被告警的那几条也列进来（休眠行的 dormancy_rank 必然排在仅到期的行前面），
    # 否则照提示写完的稿子仍会因第 3 条休眠承诺挨一条 warn。
    dormant_count = sum(1 for r in open_rows if is_dormant(r, chapter, pcfg))
    due_limit = max(limit, min(_ISSUE_CAP, dormant_count)) if limit else 0
    due = sorted((r for r in open_rows if is_due_for_reminder(r, chapter, pcfg)),
                 key=lambda r: dormancy_rank(r, chapter, pcfg))[:due_limit]
    # 超出预算的短线（craft check promise_short_overdue 的同一口径）：即使刚推进过、
    # 不在提醒周期内，也要让写手知道它该收了——否则告警无从预见。
    short_limit = short_line_limit(pcfg, language)
    overdue = overdue_short_lines(conn, open_rows, chapter, pcfg, language) if limit else []
    overdue_units = {row["id"]: units for units, row in overdue}
    lines: list[str] = []
    listed: set[str] = set()

    def overdue_suffix(row: dict) -> str:
        if row["id"] not in overdue_units:
            return ""
        return t["brief_overdue_suffix"].format(
            units=f"{overdue_units[row['id']]:,}", limit=f"{short_limit:,}")

    for row in due:
        listed.add(row["id"])
        line = t["brief_due"].format(label=_clip(row["label"], 40),
                                     planted=row["planted_chapter"],
                                     gap=chapters_since_touch(row, chapter))
        if row["description"]:
            line += t["colon"] + _clip(row["description"], desc_limit)
        line += overdue_suffix(row)
        if row["id"] in planned_verb:
            line += t["brief_planned_suffix"].format(verb=nouns[planned_verb[row["id"]]])
        lines.append(line + payoff_hints(row))

    # 契约已安排的超时短线留给下面的契约操作行（带 planner 备注 + 超时后缀）
    overdue_lines = 0
    for units, row in overdue:
        if overdue_lines >= limit:
            break
        if row["id"] in listed or row["id"] in planned_verb:
            continue
        listed.add(row["id"])
        line = t["brief_overdue"].format(label=_clip(row["label"], 40),
                                         planted=row["planted_chapter"],
                                         units=f"{units:,}", limit=f"{short_limit:,}")
        if row["description"]:
            line += t["colon"] + _clip(row["description"], desc_limit)
        lines.append(line + t["brief_overdue_tail"] + payoff_hints(row))
        overdue_lines += 1

    # 契约计划的承诺操作：契约块已列出标签，这里只补账本上下文（埋设章/描述/内层线/铺垫深度），
    # 所以只列能映射到账本、仍未关闭、且没在上面出现过的行。
    action_lines = 0
    for action, row in resolved:
        if action_lines >= _BRIEF_ACTION_LIMIT:
            break
        if row is None or not is_open(row) or row["id"] in listed:
            continue
        listed.add(row["id"])
        line = t["brief_action"].format(verb=nouns[planned_verb[row["id"]]],
                                        label=_clip(row["label"], 40))
        line += t["brief_planted"].format(planted=row["planted_chapter"])
        if row["description"]:
            line += t["colon"] + _clip(row["description"], desc_limit)
        if action.get("note"):
            line += " — " + _clip(action["note"], desc_limit)
        lines.append(line + overdue_suffix(row) + payoff_hints(row))
        action_lines += 1

    if not lines:
        return ""
    return t["brief_header"] + "\n" + "\n".join(lines)


# ── settle ───────────────────────────────────────────────────────────────────

_SAVEPOINT = "promise_ledger_settle"


def settle(conn: sqlite3.Connection, chapter: int, workspace: Any, *,
           gateway: Any = None, cfg: Any = None) -> dict:
    """Post-commit deterministic ledger step (runs after ``foreshadow_settle``).

    1. Applies the contract's ``remind`` actions whose promise appears in the
       final draft (exact phrase hit only — this path writes the ledger, so the
       fuzzy fallback used by the checks is not trusted here):
       ``last_mentioned_chapter = max(existing, chapter)`` plus one
       ``foreshadow_log`` ``mention`` row — skipped when the chapter already
       has a mention/advance/payoff row for that promise (idempotent re-runs).
       Nothing is committed here: the orchestrator commits after the step.
    2. Reports nesting violations (outer promise paid off while an inner one is
       open), the number of payoffs this chapter (and a tension-cliff
       violation above the limit, volume finales excepted), big payoffs with
       too little setup, and how many promises are dormant going into the next
       chapter.

    Never raises for data problems: they are returned under ``"error"``.
    ``gateway`` is unused (no LLM call)."""
    pcfg = resolve_config(cfg)
    report: dict[str, Any] = {"reminded": [], "violations": [], "payoffs": 0, "dormant": 0}
    if not pcfg.enabled:
        report["skipped"] = "disabled"
        return report
    ws = workspace if isinstance(workspace, Mapping) else {}
    chapter = int(chapter)
    try:
        report["reminded"] = _apply_contract_reminders(
            conn, chapter, ws, str(ws.get("draft_text") or ""),
        )
        paid = [_with_defaults(r) for r in _dicts(conn.execute(
            "SELECT * FROM foreshadow WHERE state='paid_off' AND paid_off_chapter=?"
            " ORDER BY planted_chapter, id", (chapter,),
        ))]
        report["payoffs"] = len(paid)
        report["violations"] = _settle_violations(conn, chapter, paid, pcfg, ws)
        report["dormant"] = len(dormant_promises(conn, chapter + 1, pcfg))
    except (sqlite3.Error, ValueError, TypeError) as exc:
        logger.warning("promise ledger settle failed for chapter %s: %s", chapter, exc)
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report


def _apply_contract_reminders(conn: sqlite3.Connection, chapter: int,
                              workspace: Mapping[str, Any], draft: str) -> list[dict]:
    actions = [a for a in contract_promise_actions(workspace, chapter) if a["action"] == "remind"]
    if not actions or not draft.strip():
        return []
    rows = fetch_promises(conn, as_of_chapter=chapter, open_only=False)
    matcher = DraftMatcher(draft)
    applied: list[dict] = []
    done: set[str] = set()
    # 只在调用方已开事务时用 SAVEPOINT 隔离本步写入：顶层 SAVEPOINT 的 RELEASE 等于
    # COMMIT，会抢在编排器之前提交。无事务时写入落在 DML 隐式开启的事务里，由编排器提交；
    # 出错时整体回滚（此前没有未提交的写入）。
    nested = conn.in_transaction
    if nested:
        conn.execute(f"SAVEPOINT {_SAVEPOINT}")
    try:
        for action in actions:
            row = resolve_action(action, rows)
            if row is None or row["id"] in done or not is_open(row):
                continue
            done.add(row["id"])
            evidence = _row_found(matcher, row, action.get("label"), fuzzy=False)
            if not evidence:
                continue
            already = conn.execute(
                "SELECT 1 FROM foreshadow_log WHERE foreshadow_id=? AND chapter=?"
                " AND action IN ('mention','advance','payoff') LIMIT 1",
                (row["id"], chapter),
            ).fetchone()
            if already:
                continue
            conn.execute(
                "UPDATE foreshadow SET"
                " last_mentioned_chapter=MAX(COALESCE(last_mentioned_chapter, 0), ?),"
                " updated_at=datetime('now') WHERE id=?",
                (chapter, row["id"]),
            )
            conn.execute(
                "INSERT INTO foreshadow_log(id, foreshadow_id, chapter, action, evidence)"
                " VALUES(?,?,?,'mention',?)",
                (new_id("fsl"), row["id"], chapter, evidence),
            )
            applied.append({"id": row["id"], "label": row["label"]})
        if nested:
            conn.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
    except sqlite3.Error:
        if nested:
            conn.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
            conn.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
        else:
            conn.rollback()
        raise
    return applied


def _settle_violations(conn: sqlite3.Connection, chapter: int, paid: list[dict],
                       cfg: PromiseConfig, workspace: Mapping[str, Any]) -> list[dict]:
    violations: list[dict] = []
    for row in paid:
        children = _open_children(conn, row["id"])
        if children:
            violations.append({
                "type": "nesting", "id": row["id"], "label": row["label"],
                "open_children": [{"id": c["id"], "label": c["label"]} for c in children],
            })
    limit = max(1, _int(cfg.max_payoffs_per_chapter, 2) or 2)
    if len(paid) > limit and _volume_end(workspace) != chapter:
        violations.append({
            "type": "payoff_cliff", "count": len(paid), "limit": limit,
            "ids": [r["id"] for r in paid],
        })
    need = max(1, _int(cfg.big_payoff_min_touches, 2) or 2)
    for row in paid:
        if not _is_big(row):
            continue
        touches = touch_count(conn, row, chapter)
        if touches < need:
            violations.append({
                "type": "thin_setup", "id": row["id"], "label": row["label"],
                "touches": touches, "min_touches": need,
            })
    return violations
