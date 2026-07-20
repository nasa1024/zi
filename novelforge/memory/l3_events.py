"""L3 事件驱动 canon 提案（§06.2.3 + §06.3.3）。

§06 立场：L3 不是定时任务，而是"本章出现 canon 级原子"时才触发。本实现作为
出稿期 LLM 抽取（ChapterDraftSkill→proposals）之外的**异步安全网**：
1. 确定性扫描 canon 信号（世界规则/新实体/伏笔/约束），保不漏（§06.3.3 规则三）；
2. 仅当有信号才调一次 LLM 归一化为结构化原子（绝大多数章零 LLM 成本）；
3. 证据校验（quote 必须在正文中，§06.3.1 防幻觉）；
4. 与本章既有候选去重，避免与出稿期抽取重复入库；
5. 产出 BibleChangeProposal 形状的 dict（op=add），由调用方写 staging（proposed，
   走人审兜底，绝不直接 commit——原则 2）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Mapping, Optional

from ..text_metrics import language_matches

# §06.2.3 L3 触发的四类 canon 原子
CANON_TYPES = {"world_rule", "new_entity", "foreshadowing", "constraint"}

# type → fact_candidates.fact_type.
#
# `facts.fact_type` has a strict CHECK whitelist. New entities and foreshadow
# hooks are not hard-state facets in this schema, so keep them as narrative
# `misc` facts and preserve the original semantic class in new.canon_kind.
_TYPE_TO_FACT = {
    "world_rule": "world_rule",
    "new_entity": "misc",
    "foreshadowing": "misc",
    "constraint": "constraint",
}

# 确定性信号词（召回优先，§06.3.3：发现由词表兜底，归一化交 LLM）
_WORLD_RULE_MARKERS = ("法则", "规则", "天道", "铁律", "定律", "戒律", "禁制",
                       "灵气复苏", "大道", "本源", "世界规则")
_FORESHADOW_MARKERS = ("伏笔", "预言", "谶语", "预兆", "征兆", "暗示", "隐约",
                       "日后", "多年后", "三百年后", "命中注定", "冥冥之中")
_CONSTRAINT_MARKERS = ("禁忌", "绝不得", "不得外传", "严禁", "禁止", "不可泄露",
                       "戒律", "绝不能", "违者")

_WORLD_RULE_MARKERS_EN = (
    "world rule", "law of magic", "law of the world", "immutable law",
    "magic always", "magic never", "the rule is", "it is impossible for",
)
_FORESHADOW_MARKERS_EN = (
    "prophecy", "prophesied", "omen", "foretold", "foretell", "destined",
    "one day", "someday", "years from now", "a sign of what was to come",
)
_CONSTRAINT_MARKERS_EN = (
    "forbidden", "must never", "may never", "cannot be revealed", "must not be revealed",
    "under no circumstances", "prohibited", "taboo", "on pain of death",
)

# 书名号/引号内专名（新实体候选）
_NAME_RE = re.compile(r"[《〈「『\"“]([^》〉」』\"”]{2,12})[》〉」』\"”]")
_EN_NEW_ENTITY_RE = re.compile(
    r"\b(?:called|named|known as|introduced as|the (?:order|kingdom|city|guild|academy) of)\s+"
    r"(?:the\s+)?[\"“]?([A-Z][A-Za-z'’-]*(?:\s+[A-Z][A-Za-z'’-]*|"
    r"\s+(?:of|the|and)\s+[A-Z][A-Za-z'’-]*){0,5})[\"”]?"
)

_L3_SYSTEM = (
    "你是 NovelForge 的 canon 级事件抽取器。只抽取以下四类、且本章明确出现的设定级信息："
    "world_rule（世界规则/法则）、new_entity（首次登场的人物/势力/地点）、"
    "foreshadowing（新埋的伏笔/预言）、constraint（设定级禁忌/约束）。"
    "输出 JSON 数组，每项 {type, name, statement, evidence}："
    "type 为上述四者之一；name 为相关专名（无则 null）；statement 为一句话归一化陈述；"
    "evidence 为正文中能支撑该条的原文片段（必须是正文里的连续子串）。"
    "不确定就不要抽。只输出 JSON 数组，不要其它文字。"
)

_L3_SYSTEM_EN = (
    "You are zi's canonical-event extractor. Extract only setting-level information that is "
    "explicitly established in this chapter: world_rule (a world or magic law), new_entity "
    "(a person, faction, or place first introduced here), foreshadowing (a newly planted omen "
    "or prophecy), and constraint (a canon-level prohibition or limitation). Return a JSON "
    "array whose items are {type, name, statement, evidence}. Keep these stable English keys. "
    "Write statement in natural English using the project's selected regional variant, "
    "preserve proper nouns exactly, and copy evidence as one continuous verbatim substring "
    "from the chapter. Do not infer. Return [] when evidence is insufficient. Return JSON only."
)


def scan_canon_signals(
    text: str,
    known_entity_names: Optional[set] = None,
    *,
    language: Optional[str] = None,
    writing_profile: Optional[Mapping[str, Any]] = None,
) -> list[dict]:
    """确定性扫 canon 信号。返回 [{type, anchor}]，无信号返回 []。"""
    known = known_entity_names or set()
    text = text or ""
    sigs: list[dict] = []

    profile = _normalize_explicit_profile(writing_profile)
    selected_language = str(language or profile.get("content_language") or "zh-CN")
    english = selected_language.lower().replace("_", "-").startswith("en")

    def _hit(markers, typ, *, casefold: bool = False):
        haystack = text.casefold() if casefold else text
        for m in markers:
            needle = m.casefold() if casefold else m
            if needle in haystack:
                start = haystack.find(needle)
                sigs.append({"type": typ, "anchor": text[start:start + len(m)]})
                break

    if english:
        _hit(_WORLD_RULE_MARKERS_EN, "world_rule", casefold=True)
        _hit(_FORESHADOW_MARKERS_EN, "foreshadowing", casefold=True)
        _hit(_CONSTRAINT_MARKERS_EN, "constraint", casefold=True)
        known_folded = {str(name).casefold() for name in known}
        for match in _EN_NEW_ENTITY_RE.finditer(text):
            name = match.group(1).strip().rstrip(".,;:!?")
            if name and name.casefold() not in known_folded:
                sigs.append({"type": "new_entity", "anchor": name})
    else:
        _hit(_WORLD_RULE_MARKERS, "world_rule")
        _hit(_FORESHADOW_MARKERS, "foreshadowing")
        _hit(_CONSTRAINT_MARKERS, "constraint")
        for match in _NAME_RE.finditer(text):
            name = match.group(1).strip()
            if name and name not in known:
                sigs.append({"type": "new_entity", "anchor": name})
    return sigs


def extract_canon_events(
    conn: sqlite3.Connection,
    gateway,
    chapter: int,
    draft_text: str,
    *,
    known_entity_names: Optional[set] = None,
    tier: str = "mid",
    writing_profile: Optional[Mapping[str, Any]] = None,
) -> list[dict]:
    """扫信号→（有信号才）LLM 归一化→证据校验→去重→proposal dicts。"""
    profile = _resolve_writing_profile(conn, writing_profile)
    language = str(profile.get("content_language") or "zh-CN")
    if known_entity_names is None:
        known_entity_names = _load_known_entities(conn)
    signals = scan_canon_signals(
        draft_text,
        known_entity_names,
        language=language,
        writing_profile=profile,
    )
    if not signals:
        return []          # 零信号零 LLM 成本

    atoms = _llm_normalize(
        gateway, tier, draft_text, signals, writing_profile=profile,
    )
    if not atoms:
        return []

    existing = _existing_keys(conn, chapter)
    proposals: list[dict] = []
    seen: set = set()
    for a in atoms:
        prop = _to_proposal(a, chapter, draft_text, writing_profile=profile)
        if prop is None:
            continue
        key = (prop["fact_type"], (prop.get("entity") or "").strip())
        if key in existing or key in seen:
            continue       # 与出稿期候选 / 本批内去重
        seen.add(key)
        proposals.append(prop)
    return proposals


# ── 私有 ──────────────────────────────────────────────────────────────────────

def _llm_normalize(
    gateway,
    tier: str,
    draft_text: str,
    signals: list[dict],
    *,
    writing_profile: Optional[Mapping[str, Any]] = None,
) -> list[dict]:
    from ..control_plane.llm.provider import Message
    from ..control_plane.llm.tiers import ModelTier
    from ..writing_profiles import compile_system_prompt

    mt = getattr(ModelTier, {"fast": "FAST", "mid": "MID", "strong": "STRONG"}.get(tier, "MID"))
    profile = _normalize_explicit_profile(writing_profile)
    language = str(profile.get("content_language") or "zh-CN")
    english = language.lower().replace("_", "-").startswith("en")
    hint = (", " if english else "、").join(sorted({s["type"] for s in signals}))
    user_content = (
        f"Detected possible canon signal classes: {hint}. Chapter source:\n\n{draft_text[:8000]}"
        if english else f"本章疑似含 canon 信号（{hint}）。正文：\n\n{draft_text[:6000]}"
    )
    system = compile_system_prompt(
        _L3_SYSTEM,
        profile,
        english_system=_L3_SYSTEM_EN,
    )
    try:
        resp = gateway.generate(
            mt,
            [Message(role="user", content=user_content)],
            system=system,
            max_tokens=1200 if english else 800,
        )
        return _parse_atoms(resp.text)
    except Exception:
        return []


def _parse_atoms(text: str) -> list[dict]:
    """从 LLM 文本里抠出 JSON 数组。失败返回 []。"""
    if not text:
        return []
    # ```json ... ``` 优先
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    raw = fence.group(1) if fence else None
    if raw is None:
        i, j = text.find("["), text.rfind("]")
        raw = text[i:j + 1] if (i != -1 and j > i) else None
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []


def _to_proposal(
    atom: dict,
    chapter: int,
    draft_text: str,
    *,
    writing_profile: Optional[Mapping[str, Any]] = None,
) -> Optional[dict]:
    from ..contracts import normalize_proposal_fact_type

    typ = atom.get("type")
    if typ not in CANON_TYPES:
        return None
    statement = (atom.get("statement") or "").strip()
    evidence = (atom.get("evidence") or "").strip()
    if not statement or not evidence:
        return None
    if evidence not in draft_text:           # §06.3.1 证据后验校验
        return None
    profile = _normalize_explicit_profile(writing_profile)
    language = str(profile.get("content_language") or "zh-CN")
    english = language.lower().replace("_", "-").startswith("en")
    if english and not language_matches(statement, language):
        return None
    entity = (atom.get("name") or None)
    return normalize_proposal_fact_type({
        "op": "add",
        "fact_type": _TYPE_TO_FACT[typ],
        "entity": entity,
        "new": {"statement": statement, "canon_kind": typ},
        "reason": (
            f"L3 event-driven canonical extraction ({typ})"
            if english else f"L3 事件驱动抽取（{typ}）"
        ),
        "evidence_refs": [{"chapter": chapter, "quote": evidence}],
        "valid_from_chapter": chapter,
        "risk_tier": "medium",               # canon 级 → 人审兜底
        "source": "l3",
    })


def _existing_keys(conn: sqlite3.Connection, chapter: int) -> set:
    """本章已有候选的 (fact_type, entity_name) 集，用于去重。"""
    keys: set = set()
    rows = conn.execute(
        "SELECT fact_type, entity_id, proposal_json FROM fact_candidates"
        " WHERE source_chapter=?",
        (chapter,),
    ).fetchall()
    for r in rows:
        name = ""
        try:
            p = json.loads(r["proposal_json"] or "{}")
            name = (p.get("entity") or p.get("name") or "").strip()
        except Exception:
            pass
        fact_type = r["fact_type"]
        if fact_type in {"entity", "foreshadow"}:
            fact_type = "misc"  # 兼容旧实现写入的非法 canon fact_type staging
        keys.add((fact_type, name))
    return keys


def _load_known_entities(conn: sqlite3.Connection) -> set:
    try:
        rows = conn.execute("SELECT canonical_name FROM entities").fetchall()
        return {r["canonical_name"] for r in rows if r["canonical_name"]}
    except Exception:
        return set()


def _resolve_writing_profile(
    conn: sqlite3.Connection,
    writing_profile: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    if writing_profile is not None:
        return _normalize_explicit_profile(writing_profile)
    try:
        from ..writing_profiles import get_project_writing_profile

        return get_project_writing_profile(conn)
    except Exception:
        return {"content_language": "zh-CN", "prompt_rules": {}, "quality_rules": {}}


def _normalize_explicit_profile(
    writing_profile: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    profile = dict(writing_profile or {})
    if "content_language" not in profile and profile.get("language"):
        profile["content_language"] = profile["language"]
    if "writing_profile_id" not in profile:
        profile["writing_profile_id"] = profile.get("profile_id") or profile.get("id")
    if "writing_profile_version" not in profile and profile.get("profile_version") is not None:
        profile["writing_profile_version"] = profile["profile_version"]
    profile.setdefault("content_language", "zh-CN")
    profile.setdefault("prompt_rules", {})
    profile.setdefault("quality_rules", {})
    if not profile.get("cache_key") and profile.get("writing_profile_id"):
        profile["cache_key"] = (
            f"{profile['content_language']}:{profile['writing_profile_id']}:"
            f"{profile.get('writing_profile_version', 0)}:{profile.get('prompt_version', 0)}"
        )
    return profile
