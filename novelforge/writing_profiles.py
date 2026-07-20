"""Versioned writing-profile catalog and project-level profile persistence.

The catalog is the backend source of truth for language, market, format and
style defaults.  Project databases keep a small current projection in
``meta_kv`` plus append-only snapshots in ``writing_profile_versions``.

Legacy projects intentionally remain readable without a write-on-read
migration: missing metadata resolves to the historical Chinese web-novel
profile and is exposed as revision ``0`` (``legacy``).
"""
from __future__ import annotations

import copy
import json
import sqlite3
from typing import Any, Iterable, Mapping

from .db.connection import get_meta, set_meta
from .ids import new_id


CATALOG_VERSION = "1.0.0"
LEGACY_PROFILE_ID = "cn_web_novel_v1"
DEFAULT_ENGLISH_PROFILE_ID = "en_progression_serial_v1"
SUPPORTED_CONTENT_LANGUAGES = ("zh-CN", "en-US", "en-GB")
SUPPORTED_MARKETS = ("cn_web", "global_english", "us", "uk")
SUPPORTED_FORMATS = ("web_serial", "novel")

_OVERRIDE_KEYS = {
    "pov",
    "tense",
    "pace",
    "prose_density",
    "dialogue_density",
    "chapter_word_range",
}


def _profile(
    profile_id: str,
    *,
    display_name: str,
    language: str,
    market: str,
    publication_format: str,
    description: str,
    preview: str,
    prompt_rules: Mapping[str, Any],
    chapter_word_range: tuple[int, int],
    hard_min_words: int,
    style_signals: Iterable[str] = (),
    requires_progress_event: bool = False,
) -> dict[str, Any]:
    return {
        "id": profile_id,
        "version": CATALOG_VERSION,
        "display_name": display_name,
        "language": language,
        "market": market,
        "format": publication_format,
        "description": description,
        "preview": preview,
        "prompt_rules": dict(prompt_rules),
        "quality_rules": {
            "chapter_word_range": list(chapter_word_range),
            "hard_min_words": hard_min_words,
            "max_pov_shifts": 0,
            "tense_consistency": language.startswith("en"),
            "requires_progress_event": requires_progress_event,
            "style_signals": list(style_signals),
        },
        "prompt_version": CATALOG_VERSION,
    }


_EN_PREVIEW = (
    "A chapter opens on a concrete problem, stays close to one viewpoint, and "
    "moves through a visible choice and consequence. The prose favors clear "
    "verbs, specific sensory detail, and dialogue that changes the situation. "
    "World information arrives when the character needs it instead of in a "
    "detached lecture. By the end, the immediate beat has paid off while a new "
    "question, cost, clue, or relationship turn gives the reader a reason to "
    "continue. The exact balance is adjusted by the selected profile below."
)


_PROFILES: tuple[dict[str, Any], ...] = (
    _profile(
        LEGACY_PROFILE_ID,
        display_name="中文网文",
        language="zh-CN",
        market="cn_web",
        publication_format="web_serial",
        description="保持 zi 既有中文网文生成、节奏与字符计数行为。",
        preview="以明确冲突、价值变化与章末钩子推动连载，沿用现有中文项目行为。",
        prompt_rules={
            "pov": "third_person_limited",
            "tense": "unspecified",
            "pace": "fast",
            "prose_density": "accessible",
            "dialogue_density": "medium",
            "progress_feedback": "frequent",
            "chapter_end": "forward-driving hook",
        },
        chapter_word_range=(1000, 5000),
        hard_min_words=1000,
        requires_progress_event=True,
    ),
    _profile(
        "en_progression_serial_v1",
        display_name="Progression Fantasy Serial",
        language="en-US",
        market="global_english",
        publication_format="web_serial",
        description="Visible growth, staged goals, frequent progress feedback, and forward-driving chapter endings.",
        preview=_EN_PREVIEW,
        prompt_rules={
            "pov": "third_person_limited", "tense": "past", "pace": "fast",
            "prose_density": "accessible", "dialogue_density": "medium",
            "progress_feedback": "frequent", "chapter_end": "forward-driving hook",
        },
        chapter_word_range=(1500, 2500), hard_min_words=1200,
        style_signals=("progress", "training", "mastery", "advancement", "goal"),
        requires_progress_event=True,
    ),
    _profile(
        "en_litrpg_serial_v1",
        display_name="LitRPG Serial",
        language="en-US",
        market="global_english",
        publication_format="web_serial",
        description="Readable mechanical feedback, satisfying upgrade loops, and restrained system exposition.",
        preview=_EN_PREVIEW,
        prompt_rules={
            "pov": "third_person_limited", "tense": "past", "pace": "fast",
            "prose_density": "accessible", "dialogue_density": "medium",
            "progress_feedback": "explicit", "chapter_end": "forward-driving hook",
        },
        chapter_word_range=(1500, 2500), hard_min_words=1200,
        style_signals=("level", "skill", "quest", "stat", "upgrade"),
        requires_progress_event=True,
    ),
    _profile(
        "en_epic_fantasy_v1",
        display_name="Epic Fantasy",
        language="en-US",
        market="global_english",
        publication_format="web_serial",
        description="Large-scale stakes, balanced ensemble and main plot, with richer descriptive texture.",
        preview=_EN_PREVIEW,
        prompt_rules={
            "pov": "third_person_limited", "tense": "past", "pace": "balanced",
            "prose_density": "descriptive", "dialogue_density": "medium",
            "progress_feedback": "measured", "chapter_end": "meaningful turn",
        },
        chapter_word_range=(2000, 3500), hard_min_words=1600,
        style_signals=("world", "consequence", "faction", "journey", "legacy"),
    ),
    _profile(
        "en_cozy_fantasy_v1",
        display_name="Cozy Fantasy",
        language="en-US",
        market="global_english",
        publication_format="web_serial",
        description="Lower-pressure conflict, relationship and everyday rewards, with a warm emotional register.",
        preview=_EN_PREVIEW,
        prompt_rules={
            "pov": "third_person_limited", "tense": "past", "pace": "slow_burn",
            "prose_density": "descriptive", "dialogue_density": "high",
            "progress_feedback": "relational", "chapter_end": "gentle invitation",
        },
        chapter_word_range=(1500, 3000), hard_min_words=1200,
        style_signals=("comfort", "community", "relationship", "craft", "home"),
    ),
    _profile(
        "en_grimdark_v1",
        display_name="Grimdark Fantasy",
        language="en-US",
        market="global_english",
        publication_format="web_serial",
        description="Costly choices, moral ambiguity, and controlled darkness without purposeless spectacle.",
        preview=_EN_PREVIEW,
        prompt_rules={
            "pov": "third_person_limited", "tense": "past", "pace": "balanced",
            "prose_density": "descriptive", "dialogue_density": "medium",
            "progress_feedback": "costly", "chapter_end": "consequence or dilemma",
        },
        chapter_word_range=(1800, 3000), hard_min_words=1450,
        style_signals=("cost", "consequence", "compromise", "survival", "choice"),
    ),
    _profile(
        "en_romantasy_v1",
        display_name="Romantasy",
        language="en-US",
        market="global_english",
        publication_format="web_serial",
        description="External and romantic arcs advance together through explicit relationship beats.",
        preview=_EN_PREVIEW,
        prompt_rules={
            "pov": "third_person_limited", "tense": "past", "pace": "balanced",
            "prose_density": "descriptive", "dialogue_density": "high",
            "progress_feedback": "dual_arc", "chapter_end": "relationship or plot turn",
        },
        chapter_word_range=(1800, 3000), hard_min_words=1450,
        style_signals=("trust", "attraction", "relationship", "choice", "conflict"),
    ),
    _profile(
        "en_mystery_thriller_v1",
        display_name="Mystery / Thriller",
        language="en-US",
        market="global_english",
        publication_format="web_serial",
        description="Fair clues, controlled information, escalating suspense, and earned reversals.",
        preview=_EN_PREVIEW,
        prompt_rules={
            "pov": "third_person_limited", "tense": "past", "pace": "fast",
            "prose_density": "accessible", "dialogue_density": "medium",
            "progress_feedback": "clue_driven", "chapter_end": "question, clue, or threat",
        },
        chapter_word_range=(1500, 2500), hard_min_words=1200,
        style_signals=("clue", "question", "evidence", "suspicion", "reveal"),
    ),
    _profile(
        "en_ya_speculative_v1",
        display_name="YA Speculative",
        language="en-US",
        market="global_english",
        publication_format="web_serial",
        description="Clear character voice, identity and growth themes, and age-appropriate boundaries.",
        preview=_EN_PREVIEW,
        prompt_rules={
            "pov": "first_person", "tense": "past", "pace": "balanced",
            "prose_density": "accessible", "dialogue_density": "high",
            "progress_feedback": "character_growth", "chapter_end": "choice or discovery",
        },
        chapter_word_range=(1500, 2500), hard_min_words=1200,
        style_signals=("identity", "friendship", "choice", "growth", "belonging"),
    ),
)

_PROFILE_BY_ID = {item["id"]: item for item in _PROFILES}

_GENRES: tuple[dict[str, str], ...] = (
    {"id": "xuanhuan", "language": "zh-CN", "market": "cn_web", "label": "玄幻"},
    {"id": "xianxia", "language": "zh-CN", "market": "cn_web", "label": "修仙"},
    {"id": "dushi", "language": "zh-CN", "market": "cn_web", "label": "都市"},
    {"id": "kehuan", "language": "zh-CN", "market": "cn_web", "label": "科幻"},
    {"id": "lishi", "language": "zh-CN", "market": "cn_web", "label": "历史"},
    {"id": "wuxia", "language": "zh-CN", "market": "cn_web", "label": "武侠"},
    {"id": "yanqing", "language": "zh-CN", "market": "cn_web", "label": "言情"},
    {"id": "xuanyi", "language": "zh-CN", "market": "cn_web", "label": "悬疑"},
    {"id": "default", "language": "zh-CN", "market": "cn_web", "label": "通用"},
    {"id": "fantasy", "language": "en-US", "market": "global_english", "label": "Fantasy"},
    {"id": "science_fiction", "language": "en-US", "market": "global_english", "label": "Science Fiction"},
    {"id": "romance", "language": "en-US", "market": "global_english", "label": "Romance"},
    {"id": "mystery", "language": "en-US", "market": "global_english", "label": "Mystery"},
    {"id": "thriller", "language": "en-US", "market": "global_english", "label": "Thriller"},
    {"id": "young_adult", "language": "en-US", "market": "global_english", "label": "Young Adult"},
    {"id": "speculative_fiction", "language": "en-US", "market": "global_english", "label": "Speculative Fiction"},
)


class WritingProfileError(ValueError):
    """Invalid catalog selection or profile override."""


class WritingProfileConflict(RuntimeError):
    """A requested profile change would mutate already-written history."""

    def __init__(self, code: str, message: str, **params: Any) -> None:
        self.code = code
        self.params = params
        super().__init__(message)


def list_writing_profiles(
    *,
    language: str | None = None,
    publication_format: str | None = None,
    market: str | None = None,
) -> list[dict[str, Any]]:
    items = _PROFILES
    if language:
        items = tuple(item for item in items if item["language"] == language)
    if publication_format:
        items = tuple(item for item in items if item["format"] == publication_format)
    if market:
        items = tuple(item for item in items if item["market"] == market)
    return [copy.deepcopy(item) for item in items]


def get_catalog_profile(profile_id: str) -> dict[str, Any]:
    try:
        return copy.deepcopy(_PROFILE_BY_ID[profile_id])
    except KeyError as exc:
        raise WritingProfileError(f"unknown writing profile: {profile_id}") from exc


def list_genres(*, language: str, market: str | None = None) -> list[dict[str, str]]:
    return [
        dict(item) for item in _GENRES
        if item["language"] == language and (not market or item["market"] == market)
    ]


def _json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [value]
    if not isinstance(value, (list, tuple, set)):
        raise WritingProfileError("expected a list")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text[:80])
    return result[:30]


def _json_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise WritingProfileError("invalid profile overrides JSON") from exc
    if not isinstance(value, Mapping):
        raise WritingProfileError("profile overrides must be an object")
    unknown = set(value) - _OVERRIDE_KEYS
    if unknown:
        raise WritingProfileError(f"unsupported profile overrides: {', '.join(sorted(unknown))}")
    out = dict(value)
    if "chapter_word_range" in out:
        raw_range = out["chapter_word_range"]
        if (
            not isinstance(raw_range, (list, tuple))
            or len(raw_range) != 2
            or not all(isinstance(item, int) for item in raw_range)
            or raw_range[0] < 100
            or raw_range[1] < raw_range[0]
            or raw_range[1] > 20_000
        ):
            raise WritingProfileError("chapter_word_range must be [min, max]")
        out["chapter_word_range"] = list(raw_range)
    return out


def normalize_writing_selection(selection: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(selection or {})
    language = str(raw.get("content_language") or "zh-CN")
    if language not in SUPPORTED_CONTENT_LANGUAGES:
        raise WritingProfileError(f"unsupported content language: {language}")
    if language == "en-GB":
        raise WritingProfileError("en-GB is reserved but not available in the MVP")

    default_english = language.startswith("en")
    market = str(raw.get("target_market") or ("global_english" if default_english else "cn_web"))
    publication_format = str(raw.get("publication_format") or "web_serial")
    profile_id = str(
        raw.get("writing_profile_id")
        or raw.get("style_preset_id")
        or (DEFAULT_ENGLISH_PROFILE_ID if default_english else LEGACY_PROFILE_ID)
    )
    profile = get_catalog_profile(profile_id)
    if profile["language"] != language:
        raise WritingProfileError(
            f"profile {profile_id} is for {profile['language']}, not {language}"
        )
    if profile["market"] != market:
        raise WritingProfileError(
            f"profile {profile_id} is for market {profile['market']}, not {market}"
        )
    if profile["format"] != publication_format:
        raise WritingProfileError(
            f"profile {profile_id} is for format {profile['format']}, not {publication_format}"
        )

    overrides = _json_dict(
        raw.get("writing_profile_overrides")
        if "writing_profile_overrides" in raw
        else raw.get("writing_profile_overrides_json")
    )
    prompt_rules = copy.deepcopy(profile["prompt_rules"])
    quality_rules = copy.deepcopy(profile["quality_rules"])
    for key, value in overrides.items():
        if key == "chapter_word_range":
            quality_rules[key] = value
            quality_rules["hard_min_words"] = min(
                int(quality_rules.get("hard_min_words") or value[0]), value[0]
            )
        else:
            prompt_rules[key] = value

    primary_genre = raw.get("primary_genre")
    if not primary_genre:
        primary_genre = "fantasy" if default_english else None
    if primary_genre:
        valid_genres = {
            item["id"] for item in _GENRES
            if item["language"] == language and item["market"] == market
        }
        if str(primary_genre) not in valid_genres:
            raise WritingProfileError(
                f"primary genre {primary_genre} is not available for {language}/{market}"
            )
    return {
        "content_language": language,
        "target_market": market,
        "publication_format": publication_format,
        "primary_genre": str(primary_genre) if primary_genre else None,
        "story_tags": _json_list(raw.get("story_tags") or raw.get("story_tags_json")),
        "audience_rating": str(raw.get("audience_rating") or "general"),
        "content_warnings": _json_list(
            raw.get("content_warnings") or raw.get("content_warnings_json")
        ),
        "writing_profile_id": profile_id,
        "writing_profile_catalog_version": profile["version"],
        "writing_profile_overrides": overrides,
        "display_name": profile["display_name"],
        "description": profile["description"],
        "preview": profile["preview"],
        "prompt_rules": prompt_rules,
        "quality_rules": quality_rules,
        "prompt_version": profile["prompt_version"],
    }


_META_KEYS = {
    "content_language": "content_language",
    "target_market": "target_market",
    "publication_format": "publication_format",
    "primary_genre": "primary_genre",
    "story_tags": "story_tags_json",
    "audience_rating": "audience_rating",
    "content_warnings": "content_warnings_json",
    "writing_profile_id": "writing_profile_id",
    "writing_profile_catalog_version": "writing_profile_catalog_version",
    "writing_profile_overrides": "writing_profile_overrides_json",
}


def _set_current_projection(
    conn: sqlite3.Connection,
    profile: Mapping[str, Any],
    *,
    revision: int,
    effective_from_chapter: int,
) -> None:
    for field, key in _META_KEYS.items():
        value = profile.get(field)
        if field in {"story_tags", "content_warnings", "writing_profile_overrides"}:
            value = json.dumps(value or ([] if field != "writing_profile_overrides" else {}), ensure_ascii=False)
        if value is not None:
            set_meta(conn, key, str(value))
    set_meta(conn, "writing_profile_version", str(revision))
    set_meta(conn, "writing_profile_effective_from_chapter", str(effective_from_chapter))
    set_meta(conn, "writing_prompt_version", str(profile.get("prompt_version") or CATALOG_VERSION))
    set_meta(
        conn,
        "tokenizer_version",
        _tokenizer_version_for_language(str(profile["content_language"])),
    )


def _tokenizer_version_for_language(language: str) -> str:
    try:
        from .tokenizer import tokenizer_version
        return tokenizer_version(language)
    except TypeError:  # compatibility while an older tokenizer is imported
        from .tokenizer import tokenizer_version
        return f"{language}:{tokenizer_version()}"


def _insert_snapshot(
    conn: sqlite3.Connection,
    profile: Mapping[str, Any],
    *,
    revision: int,
    effective_from_chapter: int,
    changed_by: str,
    change_reason: str | None,
) -> None:
    snapshot = dict(profile)
    snapshot["writing_profile_version"] = revision
    snapshot["effective_from_chapter"] = effective_from_chapter
    conn.execute(
        "INSERT INTO writing_profile_versions("
        "id, revision, profile_id, catalog_version, content_language, target_market,"
        " publication_format, primary_genre, story_tags_json, audience_rating,"
        " content_warnings_json, overrides_json, snapshot_json, effective_from_chapter,"
        " prompt_version, changed_by, change_reason)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            new_id("wpv"), revision, profile["writing_profile_id"],
            profile["writing_profile_catalog_version"], profile["content_language"],
            profile["target_market"], profile["publication_format"],
            profile.get("primary_genre"),
            json.dumps(profile.get("story_tags") or [], ensure_ascii=False),
            profile.get("audience_rating") or "general",
            json.dumps(profile.get("content_warnings") or [], ensure_ascii=False),
            json.dumps(profile.get("writing_profile_overrides") or {}, ensure_ascii=False),
            json.dumps(snapshot, ensure_ascii=False), effective_from_chapter,
            profile.get("prompt_version") or CATALOG_VERSION,
            changed_by, change_reason,
        ),
    )


def _begin_profile_write(conn: sqlite3.Connection, savepoint: str) -> bool:
    nested = conn.in_transaction
    if nested:
        conn.execute(f"SAVEPOINT {savepoint}")
    else:
        conn.execute("BEGIN IMMEDIATE")
    return nested


def _finish_profile_write(
    conn: sqlite3.Connection, savepoint: str, *, nested: bool, ok: bool,
) -> None:
    if nested:
        if not ok:
            conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
    elif ok:
        conn.commit()
    else:
        conn.rollback()


def initialize_project_writing_profile(
    conn: sqlite3.Connection,
    selection: Mapping[str, Any] | None,
    *,
    changed_by: str = "project_create",
) -> dict[str, Any]:
    profile = normalize_writing_selection(selection)
    revision = 1
    effective = 1
    nested = _begin_profile_write(conn, "writing_profile_initialize")
    try:
        _insert_snapshot(
            conn, profile, revision=revision, effective_from_chapter=effective,
            changed_by=changed_by, change_reason="initial profile",
        )
        _set_current_projection(
            conn, profile, revision=revision,
            effective_from_chapter=effective,
        )
    except Exception:
        _finish_profile_write(
            conn, "writing_profile_initialize", nested=nested, ok=False,
        )
        raise
    _finish_profile_write(
        conn, "writing_profile_initialize", nested=nested, ok=True,
    )
    return {**profile, "writing_profile_version": revision,
            "profile_version": revision, "effective_from_chapter": effective,
            "legacy": False}


def _load_json_meta(conn: sqlite3.Connection, key: str, fallback: Any) -> Any:
    raw = get_meta(conn, key)
    if not raw:
        return copy.deepcopy(fallback)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return copy.deepcopy(fallback)


def _finalize_resolved_profile(
    profile: dict[str, Any],
    *,
    revision: int,
    effective_from_chapter: int,
    legacy: bool,
) -> dict[str, Any]:
    profile.update({
        "writing_profile_version": revision,
        "profile_version": revision,
        "profile_id": profile["writing_profile_id"],
        "effective_from_chapter": effective_from_chapter,
        "legacy": legacy or revision == 0,
    })
    profile["cache_key"] = (
        f"{profile['content_language']}:{profile['writing_profile_id']}:"
        f"{profile['writing_profile_version']}:{profile['prompt_version']}"
    )
    return profile


def _load_profile_snapshot(value: Any) -> dict[str, Any]:
    """Validate a stored snapshot without consulting today's profile catalog."""
    if not isinstance(value, Mapping):
        raise WritingProfileError("stored writing profile snapshot must be an object")
    profile = copy.deepcopy(dict(value))
    required_strings = (
        "content_language", "target_market", "publication_format",
        "writing_profile_id", "prompt_version",
    )
    for key in required_strings:
        if not isinstance(profile.get(key), str) or not profile[key].strip():
            raise WritingProfileError(f"stored writing profile snapshot lacks {key}")
    if not isinstance(profile.get("prompt_rules"), Mapping):
        raise WritingProfileError("stored writing profile snapshot lacks prompt_rules")
    if not isinstance(profile.get("quality_rules"), Mapping):
        raise WritingProfileError("stored writing profile snapshot lacks quality_rules")
    profile["prompt_rules"] = dict(profile["prompt_rules"])
    profile["quality_rules"] = dict(profile["quality_rules"])
    profile["story_tags"] = _json_list(profile.get("story_tags"))
    profile["content_warnings"] = _json_list(profile.get("content_warnings"))
    overrides = profile.get("writing_profile_overrides") or {}
    if not isinstance(overrides, Mapping):
        raise WritingProfileError("stored writing profile overrides must be an object")
    profile["writing_profile_overrides"] = dict(overrides)
    profile.setdefault(
        "writing_profile_catalog_version",
        str(profile.get("catalog_version") or "legacy"),
    )
    profile.setdefault("display_name", profile["writing_profile_id"])
    profile.setdefault("description", "")
    profile.setdefault("preview", "")
    profile.setdefault("audience_rating", "general")
    profile.setdefault("primary_genre", None)
    return profile


def _is_missing_profile_versions_table(exc: sqlite3.OperationalError) -> bool:
    """Only treat a genuinely pre-migration database as a legacy project."""
    message = str(exc).lower()
    return "no such table" in message and "writing_profile_versions" in message


def _profile_for_chapter(
    conn: sqlite3.Connection, chapter: int,
) -> dict[str, Any] | None:
    """Resolve the newest append-only snapshot effective for ``chapter``."""
    try:
        row = conn.execute(
            "SELECT revision, effective_from_chapter, snapshot_json"
            " FROM writing_profile_versions"
            " WHERE effective_from_chapter<=?"
            " ORDER BY revision DESC LIMIT 1",
            (chapter,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if _is_missing_profile_versions_table(exc):
            return None
        raise
    if row is None:
        return None
    try:
        snapshot = json.loads(row["snapshot_json"])
        profile = _load_profile_snapshot(snapshot)
    except (json.JSONDecodeError, TypeError, WritingProfileError) as exc:
        raise WritingProfileError("corrupt stored writing profile snapshot") from exc
    return _finalize_resolved_profile(
        profile,
        revision=int(row["revision"]),
        effective_from_chapter=int(row["effective_from_chapter"]),
        legacy=False,
    )


def _latest_profile_snapshot(conn: sqlite3.Connection) -> dict[str, Any] | None:
    try:
        row = conn.execute(
            "SELECT revision, effective_from_chapter, snapshot_json"
            " FROM writing_profile_versions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if _is_missing_profile_versions_table(exc):
            return None
        raise
    if row is None:
        return None
    try:
        profile = _load_profile_snapshot(json.loads(row["snapshot_json"]))
    except (json.JSONDecodeError, TypeError, WritingProfileError) as exc:
        raise WritingProfileError("corrupt stored writing profile snapshot") from exc
    return _finalize_resolved_profile(
        profile,
        revision=int(row["revision"]),
        effective_from_chapter=int(row["effective_from_chapter"]),
        legacy=False,
    )


def get_project_writing_profile(
    conn: sqlite3.Connection,
    *,
    chapter: int | None = None,
) -> dict[str, Any]:
    """Return the latest profile, or the snapshot effective for a target chapter.

    Settings/API reads omit ``chapter`` and see the newest configured version.
    Generation paths pass their target chapter so a version scheduled farther in
    the future cannot affect intervening drafts.
    """
    if chapter is not None:
        resolved = _profile_for_chapter(conn, max(1, int(chapter)))
        if resolved is not None:
            return resolved
    else:
        latest_snapshot = _latest_profile_snapshot(conn)
        if latest_snapshot is not None:
            return latest_snapshot

    language = get_meta(conn, "content_language")
    legacy = not language
    raw: dict[str, Any] = {
        "content_language": language or "zh-CN",
        "target_market": get_meta(conn, "target_market"),
        "publication_format": get_meta(conn, "publication_format"),
        "primary_genre": get_meta(conn, "primary_genre"),
        "story_tags": _load_json_meta(conn, "story_tags_json", []),
        "audience_rating": get_meta(conn, "audience_rating"),
        "content_warnings": _load_json_meta(conn, "content_warnings_json", []),
        "writing_profile_id": get_meta(conn, "writing_profile_id"),
        "writing_profile_overrides": _load_json_meta(conn, "writing_profile_overrides_json", {}),
    }
    try:
        profile = normalize_writing_selection(raw)
    except WritingProfileError:
        # Corrupt/retired catalog ids must not make an old project unreadable.
        profile = normalize_writing_selection({"content_language": "zh-CN"})
        legacy = True
    revision_raw = get_meta(conn, "writing_profile_version")
    try:
        revision = int(revision_raw) if revision_raw is not None else 0
    except ValueError:
        revision = 0
    effective_raw = get_meta(conn, "writing_profile_effective_from_chapter")
    try:
        effective = int(effective_raw) if effective_raw is not None else 1
    except ValueError:
        effective = 1
    if chapter is not None and revision > 0 and effective > chapter:
        # Pre-feature projects have no revision-0 snapshot.  Before their first
        # scheduled profile takes effect, preserve the deterministic legacy
        # Chinese behavior rather than applying the future projection early.
        profile = normalize_writing_selection({"content_language": "zh-CN"})
        revision, effective, legacy = 0, 1, True
    return _finalize_resolved_profile(
        profile,
        revision=revision,
        effective_from_chapter=effective,
        legacy=legacy,
    )


def profile_prompt_block(profile: Mapping[str, Any]) -> str:
    """Stable profile prefix included in every creative prompt/cache identity."""
    rules = profile.get("prompt_rules") or {}
    quality = profile.get("quality_rules") or {}
    if str(profile.get("content_language") or "").startswith("en"):
        return (
            "## Locked Writing Profile\n"
            f"Cache identity: {profile.get('cache_key') or profile.get('writing_profile_id')}\n"
            "All user-visible creative content and fallback content MUST be natural American English.\n"
            f"Market: {profile.get('target_market')}; format: {profile.get('publication_format')}; "
            f"profile: {profile.get('writing_profile_id')} revision {profile.get('writing_profile_version', 0)}.\n"
            f"POV: {rules.get('pov')}; tense: {rules.get('tense')}; pace: {rules.get('pace')}; "
            f"prose: {rules.get('prose_density')}; dialogue density: {rules.get('dialogue_density')}.\n"
            f"Chapter length: {quality.get('chapter_word_range')} words; ending: {rules.get('chapter_end')}.\n"
            "Treat these as hard project constraints. Preserve stable JSON keys in English."
        )
    return (
        "## 项目写作配置（锁定）\n"
        f"缓存标识：{profile.get('cache_key') or profile.get('writing_profile_id')}\n"
        f"作品语言：简体中文；配置：{profile.get('writing_profile_id')}；"
        f"节奏：{rules.get('pace')}；章末：{rules.get('chapter_end')}。"
    )


def compile_system_prompt(
    chinese_system: str,
    profile: Mapping[str, Any],
    *,
    english_system: str | None = None,
) -> str:
    base = english_system if (
        str(profile.get("content_language") or "").startswith("en") and english_system
    ) else chinese_system
    return f"{base.rstrip()}\n\n{profile_prompt_block(profile)}"


def list_project_profile_history(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT id, revision, profile_id, catalog_version, content_language,"
            " target_market, publication_format, primary_genre, story_tags_json,"
            " audience_rating, content_warnings_json, overrides_json, snapshot_json,"
            " effective_from_chapter, prompt_version, changed_by, change_reason, created_at"
            " FROM writing_profile_versions ORDER BY revision DESC"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if _is_missing_profile_versions_table(exc):
            return []
        raise
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key, fallback in (
            ("story_tags_json", []), ("content_warnings_json", []),
            ("overrides_json", {}), ("snapshot_json", {}),
        ):
            try:
                item[key.removesuffix("_json")] = json.loads(item.pop(key) or "null") or fallback
            except json.JSONDecodeError:
                item[key.removesuffix("_json")] = fallback
        result.append(item)
    return result


def update_project_writing_profile(
    conn: sqlite3.Connection,
    patch: Mapping[str, Any],
    *,
    changed_by: str = "user",
    change_reason: str | None = None,
    confirm_high_risk: bool = False,
    effective_from_chapter: int | None = None,
) -> dict[str, Any]:
    if not patch and effective_from_chapter is None:
        current = get_project_writing_profile(conn)
        current["history"] = list_project_profile_history(conn)
        return current

    nested = _begin_profile_write(conn, "writing_profile_update")
    try:
        current = get_project_writing_profile(conn)
        latest = conn.execute(
            "SELECT COALESCE(MAX(chapter), 0) FROM draft_index"
        ).fetchone()[0]

        requested_language = patch.get("content_language")
        if (
            requested_language
            and requested_language != current["content_language"]
            and latest > 0
        ):
            raise WritingProfileConflict(
                "content_language_migration_required",
                "已有正文后不能直接修改作品语言；MVP 不支持自动语言迁移。",
                latest_chapter=latest,
            )

        selection = {
            "content_language": current["content_language"],
            "target_market": current["target_market"],
            "publication_format": current["publication_format"],
            "primary_genre": current.get("primary_genre"),
            "story_tags": current.get("story_tags") or [],
            "audience_rating": current.get("audience_rating"),
            "content_warnings": current.get("content_warnings") or [],
            "writing_profile_id": current["writing_profile_id"],
            "writing_profile_overrides": (
                current.get("writing_profile_overrides") or {}
            ),
        }
        allowed = set(selection)
        for key, value in patch.items():
            if key == "primary_genre":
                selection[key] = value
            elif key in allowed and value is not None:
                selection[key] = value
        if requested_language and requested_language != current["content_language"]:
            selection["content_language"] = requested_language
            if "target_market" not in patch:
                selection["target_market"] = (
                    "global_english"
                    if str(requested_language).startswith("en") else "cn_web"
                )
            if "writing_profile_id" not in patch:
                selection["writing_profile_id"] = (
                    DEFAULT_ENGLISH_PROFILE_ID
                    if str(requested_language).startswith("en")
                    else LEGACY_PROFILE_ID
                )
        updated = normalize_writing_selection(selection)

        old_rules = current.get("prompt_rules") or {}
        new_rules = updated.get("prompt_rules") or {}
        high_risk_changed = any(
            old_rules.get(key) != new_rules.get(key)
            for key in ("pov", "tense")
        )
        if latest > 0 and high_risk_changed and not confirm_high_risk:
            raise WritingProfileConflict(
                "high_risk_confirmation_required",
                "已有正文后修改 POV 或时态需要二次确认，并只从下一章生效。",
                effective_from_chapter=latest + 1,
            )

        effective = (
            effective_from_chapter
            if effective_from_chapter is not None
            else (latest + 1 if latest > 0 else 1)
        )
        if latest > 0 and effective <= latest:
            raise WritingProfileConflict(
                "effective_chapter_in_past",
                "写作配置只能从下一章或更晚的章节生效。",
                latest_chapter=latest,
                minimum_effective_from=latest + 1,
            )
        max_revision = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM writing_profile_versions"
        ).fetchone()[0]
        revision = max(
            int(max_revision or 0),
            int(current.get("writing_profile_version") or 0),
        ) + 1
        _insert_snapshot(
            conn,
            updated,
            revision=revision,
            effective_from_chapter=effective,
            changed_by=changed_by,
            change_reason=change_reason,
        )
        _set_current_projection(
            conn,
            updated,
            revision=revision,
            effective_from_chapter=effective,
        )
    except Exception:
        _finish_profile_write(
            conn, "writing_profile_update", nested=nested, ok=False,
        )
        raise
    _finish_profile_write(
        conn, "writing_profile_update", nested=nested, ok=True,
    )
    result = get_project_writing_profile(conn)
    result["history"] = list_project_profile_history(conn)
    return result
