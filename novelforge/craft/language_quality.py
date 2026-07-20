"""Deterministic English chapter quality checks.

The checks in this module are intentionally conservative.  They catch
mechanical profile violations and return source-anchored findings; literary
judgement remains advisory and readability can never block a chapter.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..text_metrics import count_words, iter_words, language_matches


_SENTENCE_RE = re.compile(r"[^.!?\n]+(?:[.!?]+|$)", re.MULTILINE)
_DIALOGUE_RE = re.compile(
    r'"[^"\n]*"|“[^”\n]*”|「[^」\n]*」|『[^』\n]*』|‘[^’\n]*’'
)
_SPEAKER_ATTRIBUTION_RE = re.compile(
    r"[\"”]\s*(?:(?P<speaker1>[A-Z][A-Za-z'-]+|he|she|they)\s+"
    r"(?:said|asked|replied|answered|whispered|shouted)|"
    r"(?:said|asked|replied|answered|whispered|shouted)\s+"
    r"(?P<speaker2>[A-Z][A-Za-z'-]+|he|she|they))\b",
    re.I,
)
_FIRST_PERSON_RE = re.compile(r"\b(?:I|me|my|mine|myself|we|us|our|ours|ourselves)\b", re.I)

_PAST_MARKERS = {
    "was", "were", "had", "did", "went", "came", "said", "saw", "felt",
    "knew", "thought", "took", "made", "found", "gave", "stood", "sat",
    "ran", "brought", "became", "left", "heard", "held", "kept", "told",
}
_PRESENT_MARKERS = {
    "am", "is", "are", "has", "have", "do", "does", "go", "goes", "come",
    "comes", "say", "says", "see", "sees", "feel", "feels", "know", "knows",
    "think", "thinks", "take", "takes", "make", "makes", "find", "finds",
    "give", "gives", "stand", "stands", "sit", "sits", "run", "runs",
    "become", "becomes", "leave", "leaves", "hear", "hears", "hold", "holds",
}

# Deliberately small, high-confidence spelling map.  Ambiguous vocabulary is
# omitted to avoid treating an author's valid lexical choice as an error.
_US_TO_GB = {
    "color": "colour", "colors": "colours", "colored": "coloured",
    "favor": "favour", "favors": "favours", "favorite": "favourite",
    "honor": "honour", "honors": "honours", "honored": "honoured",
    "labor": "labour", "neighbor": "neighbour", "behavior": "behaviour",
    "center": "centre", "centers": "centres", "meter": "metre",
    "theater": "theatre", "gray": "grey", "traveling": "travelling",
    "traveled": "travelled", "defense": "defence", "offense": "offence",
    "license": "licence", "analyze": "analyse", "catalog": "catalogue",
}
_GB_TO_US = {gb: us for us, gb in _US_TO_GB.items()}

_PROGRESS_RE = re.compile(
    r"\b(?:advance(?:d|s)?|achiev(?:e|ed|es)|complete(?:d|s)?|gain(?:ed|s)?|"
    r"grow(?:s|n)?|improv(?:e|ed|es)|learn(?:ed|t|s)?|level(?:ed|s)?\s+up|"
    r"progress(?:ed|es)?|promot(?:e|ed|es)|rank(?:ed|s)?\s+up|reach(?:ed|es)|"
    r"unlock(?:ed|s)?)\b",
    re.I,
)
_FORWARD_HOOK_RE = re.compile(
    r"\?|\b(?:but|however|until|suddenly|then|tonight|tomorrow|next|before\s+he\s+could|"
    r"before\s+she\s+could|a\s+knock|a\s+voice|the\s+door\s+opened|wait(?:ed|ing)?\s+for)\b|[—–-]\s*$",
    re.I,
)
_STYLE_SIGNAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "progress": _PROGRESS_RE,
    "advancement": re.compile(r"\badvanc(?:e|ed|es|ement)\b", re.I),
    "training": re.compile(r"\b(?:train(?:ed|ing|s)?|practi[cs](?:e|ed|ing|s)?)\b", re.I),
    "mastery": re.compile(r"\bmaster(?:y|ed|ing|s)?\b", re.I),
    "goal": re.compile(r"\b(?:goal|objective|aim|needed?\s+to|had\s+to|must)\b", re.I),
    "relationship": re.compile(r"\b(?:relationship|trust|bond|friend(?:ship)?|ally|partner)\b", re.I),
    "character_growth": re.compile(r"\b(?:realiz(?:e|ed|es)|accept(?:ed|s)?|change(?:d|s)?|grow(?:n|s)?)\b", re.I),
    "clue": re.compile(r"\b(?:clue|evidence|trace|lead|deduc(?:e|ed|tion))\b", re.I),
    "question": re.compile(r"\?|\b(?:question|mystery|unknown|why|how)\b", re.I),
    "consequence": re.compile(r"\b(?:consequence|cost|price|aftermath|because|therefore)\b", re.I),
}

_WARNING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("graphic_violence", re.compile(
        r"\b(?:blood\s+(?:sprayed|spurted|gushed)|guts?|gore|decapitat(?:e|ed|ion)|"
        r"severed\s+(?:head|limb|arm|leg)|skull\s+(?:split|cracked)|intestines?)\b", re.I)),
    ("profanity", re.compile(
        r"\b(?:fuck(?:ed|ing|er|ers)?|shit(?:ty|ting)?|bitch(?:es)?|cunt(?:s)?|motherfucker(?:s)?)\b",
        re.I)),
    ("sexual_content", re.compile(
        r"\b(?:explicit\s+sex|sexual\s+intercourse|penetrat(?:e|ed|ion)|orgasm(?:ed|s)?)\b", re.I)),
    ("self_harm", re.compile(
        r"\b(?:suicide|suicidal|kill\s+(?:myself|himself|herself|themselves)|"
        r"cut\s+(?:my|his|her|their)\s+wrists?)\b", re.I)),
)

_STOPWORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "he", "her",
    "his", "i", "in", "is", "it", "of", "on", "or", "she", "that", "the",
    "their", "they", "this", "to", "was", "we", "were", "with", "you",
}


@dataclass(frozen=True)
class WordLimits:
    hard_min: int
    target_min: int
    target_max: int


def english_word_limits(profile: Mapping[str, Any] | None) -> WordLimits:
    """Resolve hard and target word limits from a writing-profile dictionary."""

    profile = profile or {}
    quality = _mapping(profile.get("quality_rules"))
    raw_range = quality.get("chapter_word_range") or profile.get("chapter_word_range")
    target_min, target_max = 1500, 2500
    if isinstance(raw_range, Sequence) and not isinstance(raw_range, (str, bytes)) and len(raw_range) >= 2:
        try:
            left, right = int(raw_range[0]), int(raw_range[1])
            if left > 0 and right >= left:
                target_min, target_max = left, right
        except (TypeError, ValueError):
            pass

    hard_raw = next((quality.get(key) for key in (
        "hard_min_words", "hard_min", "chapter_word_hard_min", "hard_min_word_count",
        "word_count_hard_min", "min_word_count",
    ) if quality.get(key) is not None), None)
    try:
        hard_min = int(hard_raw) if hard_raw is not None else max(1, math.floor(target_min * 2 / 3))
    except (TypeError, ValueError):
        hard_min = max(1, math.floor(target_min * 2 / 3))
    hard_min = max(1, min(hard_min, target_min))
    return WordLimits(hard_min=hard_min, target_min=target_min, target_max=target_max)


def check_english_quality(
    text: str,
    profile: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return source-anchored deterministic findings for an English chapter.

    The caller decides whether to run this function.  Supplying a non-English
    profile returns an empty list so legacy Chinese behaviour is unchanged.
    """

    profile = profile or {}
    language = str(profile.get("content_language") or profile.get("language") or "")
    if not language.lower().replace("_", "-").startswith("en"):
        return []

    source = text or ""
    prompt = _mapping(profile.get("prompt_rules"))
    quality = _mapping(profile.get("quality_rules"))
    findings: list[dict[str, Any]] = []

    if source.strip() and not language_matches(source, language):
        evidence = _script_mismatch_evidence(source)
        findings.append(_finding(
            "language_match", "block",
            f"The chapter's dominant script does not match {language}.",
            evidence,
            f"Rewrite the chapter in {language}; keep only established proper nouns in another language.",
            repair_scope="structural",
        ))

    findings.extend(_check_word_count(source, profile))
    findings.extend(_check_pov(source, prompt, quality))
    findings.extend(_check_tense(source, prompt, quality))
    findings.extend(_check_spelling(source, language))
    findings.extend(_check_dialogue_format(source, language))
    findings.extend(_check_style(source, prompt, quality))
    findings.extend(_check_repetition(source))
    findings.extend(_check_readability(source, prompt, quality))
    findings.extend(_check_content_warnings(source, profile, prompt, quality))
    return findings


def profile_adherence_score(findings: Iterable[Mapping[str, Any]]) -> float:
    """Convert deterministic findings into a stable 0..1 selection score."""

    penalty = 0.0
    for finding in findings:
        category = str(finding.get("category") or "")
        severity = finding.get("severity")
        if severity == "block":
            penalty += 0.30
        elif category == "readability":
            penalty += 0.02
        else:
            penalty += 0.07
    return round(max(0.0, 1.0 - penalty), 4)


def _check_word_count(text: str, profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    limits = english_word_limits(profile)
    words = count_words(text)
    if limits.target_min <= words <= limits.target_max:
        return []
    evidence = _edge_excerpt(text)
    if words < limits.hard_min:
        severity = "block"
        issue = (
            f"Chapter has {words} words, below the hard minimum of {limits.hard_min} "
            f"(target {limits.target_min}-{limits.target_max})."
        )
    else:
        severity = "warn"
        issue = f"Chapter has {words} words; target range is {limits.target_min}-{limits.target_max}."
    return [_finding(
        "word_count_range", severity, issue, evidence,
        f"Revise toward {limits.target_min}-{limits.target_max} words without padding or cutting required beats.",
    )]


def _check_pov(text: str, prompt: Mapping[str, Any], quality: Mapping[str, Any]) -> list[dict[str, Any]]:
    expected = str(prompt.get("pov") or quality.get("pov") or "").lower()
    sentences = _narrative_sentences(text)
    if expected in {"third_person_limited", "third-person-limited", "third person limited"}:
        hits = [original for original, narrative in sentences if _FIRST_PERSON_RE.search(narrative)]
        threshold = 1 if _safe_int(quality.get("max_pov_shifts"), 1) == 0 else 2
        if len(hits) < threshold:
            return []
        return [_finding(
            "pov_consistency", "warn",
            "First-person narration appears in a third-person-limited chapter.",
            hits[0],
            "Rewrite the out-of-POV narration through the selected viewpoint character.",
            repair_scope="structural",
        )]
    if expected in {"first_person", "first-person", "first person"}:
        narrative = " ".join(item[1] for item in sentences)
        if count_words(narrative) >= 20 and not _FIRST_PERSON_RE.search(narrative):
            return [_finding(
                "pov_consistency", "warn",
                "No first-person narrator signal appears in a chapter configured for first person.",
                sentences[0][0] if sentences else _edge_excerpt(text),
                "Anchor the narration in the viewpoint character's first-person experience.",
                repair_scope="structural",
            )]
    return []


def _check_tense(text: str, prompt: Mapping[str, Any], quality: Mapping[str, Any]) -> list[dict[str, Any]]:
    if quality.get("tense_consistency") is False:
        return []
    expected = str(prompt.get("tense") or quality.get("tense") or "").lower()
    if expected not in {"past", "present", "past_tense", "present_tense"}:
        return []
    expected_past = expected.startswith("past")
    expected_count = unexpected_count = 0
    evidence = ""
    for original, narrative in _narrative_sentences(text):
        words = [m.group(0).casefold() for m in iter_words(narrative)]
        past = sum(1 for word in words if word in _PAST_MARKERS or (len(word) > 3 and word.endswith("ed")))
        present = sum(1 for word in words if word in _PRESENT_MARKERS)
        wanted, unwanted = (past, present) if expected_past else (present, past)
        expected_count += wanted
        unexpected_count += unwanted
        if unwanted and not evidence:
            evidence = original
    total = expected_count + unexpected_count
    if unexpected_count < 2 or (total and unexpected_count / total < 0.25):
        return []
    label = "past" if expected_past else "present"
    return [_finding(
        "tense_consistency", "warn",
        f"Narrative verb tense shifts away from the configured {label} tense.",
        evidence or _edge_excerpt(text),
        f"Keep narration in {label} tense except for deliberately framed dialogue or flashback passages.",
        repair_scope="structural",
    )]


def _check_spelling(text: str, language: str) -> list[dict[str, Any]]:
    locale = language.lower().replace("_", "-")
    unwanted = _GB_TO_US if locale.startswith("en-us") else _US_TO_GB if locale.startswith("en-gb") else {}
    if not unwanted:
        return []
    mismatches: list[tuple[str, str]] = []
    for match in iter_words(text):
        replacement = unwanted.get(match.group(0).casefold())
        if replacement:
            mismatches.append((match.group(0), replacement))
    if not mismatches:
        return []
    word, replacement = mismatches[0]
    return [_finding(
        "spelling_variant", "warn",
        f"Found {len(mismatches)} spelling choice(s) inconsistent with {language}; "
        f"'{word}' should use the configured variant.",
        word,
        f"Use '{replacement}' and normalize the other flagged spellings to {language}.",
    )]


def _check_dialogue_format(text: str, language: str) -> list[dict[str, Any]]:
    problems: list[tuple[str, str]] = []
    corner = re.search(r"[「」『』]", text)
    if corner:
        problems.append((corner.group(0), "CJK corner quotes are used in English dialogue."))
    if text.count('"') % 2:
        problems.append((_line_around(text, text.find('"')), "A straight double quote is unmatched."))
    if text.count("“") != text.count("”"):
        idx = min((i for i in (text.find("“"), text.find("”")) if i >= 0), default=0)
        problems.append((_line_around(text, idx), "Curly double quotation marks are unmatched."))
    if language.lower().replace("_", "-").startswith("en-us"):
        single_dialogue = re.search(r"(?m)^\s*(?:‘[^’\n]{3,}’|'[^'\n]{3,}')", text)
        if single_dialogue:
            problems.append((single_dialogue.group(0).strip(),
                             "Dialogue uses single quotation marks in an en-US profile."))
    for paragraph in re.split(r"\n\s*\n|\n", text):
        speakers = {
            (match.group("speaker1") or match.group("speaker2") or "").casefold()
            for match in _SPEAKER_ATTRIBUTION_RE.finditer(paragraph)
        }
        speakers.discard("")
        if len(speakers) >= 2:
            problems.append((
                paragraph.strip()[:300],
                "An explicit speaker change occurs without starting a new paragraph.",
            ))
            break
    if not problems:
        return []
    evidence, issue = problems[0]
    return [_finding(
        "dialogue_format", "warn", issue, evidence,
        "Use balanced English quotation marks and start a new paragraph when the speaker changes.",
    )]


def _check_style(text: str, prompt: Mapping[str, Any], quality: Mapping[str, Any]) -> list[dict[str, Any]]:
    problems: list[str] = []
    evidence = ""
    requires_progress = bool(quality.get("requires_progress_event")) or str(
        prompt.get("progress_feedback") or "").lower() in {"frequent", "required"}
    if requires_progress and not _PROGRESS_RE.search(text):
        problems.append("the configured progression feedback is missing")
        evidence = _edge_excerpt(text, from_end=True)

    ending_rule = str(prompt.get("chapter_end") or quality.get("chapter_end") or "").lower()
    requires_hook = bool(quality.get("requires_forward_hook")) or any(
        term in ending_rule for term in ("hook", "forward", "cliffhanger")
    )
    ending = _last_sentence(text)
    if requires_hook and ending and not _FORWARD_HOOK_RE.search(ending):
        problems.append("the chapter ending does not create clear forward momentum")
        evidence = evidence or ending

    pace = str(prompt.get("pace") or "").lower()
    sentence_lengths = [count_words(original) for original, _ in _narrative_sentences(text)]
    if pace == "fast" and sentence_lengths and sum(sentence_lengths) / len(sentence_lengths) > 30:
        problems.append("sentence structure is unusually dense for the configured fast pace")
        evidence = evidence or _longest_sentence(text)

    required_terms = quality.get("style_required_terms") or prompt.get("style_required_terms") or []
    if isinstance(required_terms, Sequence) and not isinstance(required_terms, (str, bytes)):
        missing = [str(term) for term in required_terms if str(term).casefold() not in text.casefold()]
        if missing:
            problems.append("required profile signal(s) are absent: " + ", ".join(missing[:3]))
            evidence = evidence or _edge_excerpt(text, from_end=True)

    style_signals = quality.get("style_signals") or []
    if isinstance(style_signals, Sequence) and not isinstance(style_signals, (str, bytes)):
        signals = [str(signal).casefold() for signal in style_signals if str(signal).strip()]
        if signals and not any(_style_signal_visible(signal, text) for signal in signals):
            problems.append("none of the configured style signals are visible")
            evidence = evidence or _edge_excerpt(text, from_end=True)

    if not problems:
        return []
    return [_finding(
        "style_adherence", "warn",
        "Profile adherence issue: " + "; ".join(problems) + ".",
        evidence or _edge_excerpt(text),
        "Revise the cited passage to express the profile rule through story action, not added exposition.",
    )]


def _check_repetition(text: str) -> list[dict[str, Any]]:
    openings: dict[tuple[str, ...], list[str]] = {}
    for original, _ in _narrative_sentences(text):
        matches = list(iter_words(original))
        words = tuple(m.group(0).casefold() for m in matches[:3])
        if len(words) < 3 or all(word in _STOPWORDS for word in words):
            continue
        openings.setdefault(words, []).append(original)
    repeated = [(key, spans) for key, spans in openings.items() if len(spans) >= 3]
    if repeated:
        key, spans = max(repeated, key=lambda item: len(item[1]))
        phrase = _exact_phrase(text, key) or " ".join(key)
        return [_finding(
            "repetition", "warn",
            f"The sentence opening '{phrase}' repeats {len(spans)} times.",
            phrase,
            "Vary sentence openings or combine repeated actions while preserving intentional motifs.",
        )]

    template_patterns = (
        r"\bfor a moment\b", r"\bcould(?:n't| not) help but\b",
        r"\ba shiver ran down\b", r"\blittle did \w+ know\b",
    )
    for raw in template_patterns:
        matches = list(re.finditer(raw, text, re.I))
        if len(matches) >= 3:
            evidence = matches[0].group(0)
            return [_finding(
                "repetition", "warn",
                f"The stock phrase '{evidence}' repeats {len(matches)} times.",
                evidence,
                "Keep the strongest occurrence and replace the others with specific action or sensation.",
            )]
    return []


def _check_readability(
    text: str,
    prompt: Mapping[str, Any],
    quality: Mapping[str, Any],
) -> list[dict[str, Any]]:
    words = [m.group(0) for m in iter_words(text) if any(ch.isalpha() for ch in m.group(0))]
    min_words = _safe_int(quality.get("readability_min_words"), 30)
    if len(words) < max(10, min_words):
        return []
    sentences = list(_narrative_sentences(text))
    sentence_count = max(1, len(sentences))
    syllables = sum(_estimate_syllables(word) for word in words)
    ease = 206.835 - 1.015 * (len(words) / sentence_count) - 84.6 * (syllables / len(words))
    density = str(prompt.get("prose_density") or "accessible").lower()
    threshold = {"accessible": 45.0, "descriptive": 30.0, "literary": 10.0}.get(density, 35.0)
    if ease >= threshold:
        return []
    return [_finding(
        "readability", "warn",
        f"Estimated reading ease is {ease:.1f}, below the advisory {threshold:.0f} threshold "
        f"for {density} prose.",
        _longest_sentence(text),
        "Consider shortening the densest sentence or replacing unnecessary abstractions; do not flatten intentional style.",
    )]


def _check_content_warnings(
    text: str,
    profile: Mapping[str, Any],
    prompt: Mapping[str, Any],
    quality: Mapping[str, Any],
) -> list[dict[str, Any]]:
    declared_raw = (
        profile.get("content_warnings")
        or quality.get("content_warnings")
        or prompt.get("content_warnings")
        or []
    )
    if isinstance(declared_raw, str):
        declared = {declared_raw.casefold()}
    elif isinstance(declared_raw, Iterable):
        declared = {str(item).casefold() for item in declared_raw}
    else:
        declared = set()
    aliases = {
        "violence": "graphic_violence", "graphic violence": "graphic_violence",
        "strong_language": "profanity", "strong language": "profanity",
        "sexual content": "sexual_content", "self harm": "self_harm",
    }
    declared = {aliases.get(item, item) for item in declared}
    for category, pattern in _WARNING_PATTERNS:
        match = pattern.search(text)
        if match and category not in declared:
            return [_finding(
                "content_warning_match", "warn",
                f"The chapter appears to contain {category.replace('_', ' ')}, but that warning is not declared.",
                match.group(0),
                f"Review the passage and add '{category}' to the project's content warnings if appropriate.",
            )]
    return []


def _finding(
    category: str,
    severity: str,
    issue: str,
    evidence: str,
    fix: str,
    *,
    repair_scope: str = "local",
) -> dict[str, Any]:
    evidence = str(evidence or "")[:300]
    issue = str(issue or "")[:500]
    fix = str(fix or "")[:200]
    return {
        "check": category,
        "category": category,
        "severity": "block" if severity == "block" else "warn",
        "issue": issue,
        "detail": issue,
        "evidence": evidence,
        "span": evidence,
        "fix": fix,
        "repair_scope": repair_scope,
        "source": "english_deterministic",
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _narrative_sentences(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for match in _SENTENCE_RE.finditer(text or ""):
        original = match.group(0).strip()
        if not original:
            continue
        narrative = _DIALOGUE_RE.sub(" ", original)
        out.append((original, narrative))
    return out


def _last_sentence(text: str) -> str:
    sentences = _narrative_sentences(text)
    return sentences[-1][0] if sentences else _edge_excerpt(text, from_end=True)


def _longest_sentence(text: str) -> str:
    sentences = [item[0] for item in _narrative_sentences(text)]
    return max(sentences, key=count_words) if sentences else _edge_excerpt(text)


def _edge_excerpt(text: str, *, from_end: bool = False, limit: int = 180) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[-limit:] if from_end else value[:limit]


def _script_mismatch_evidence(text: str) -> str:
    match = re.search(r"[\u3400-\u9fff][^\n]{0,120}", text)
    return match.group(0) if match else _edge_excerpt(text)


def _line_around(text: str, index: int, limit: int = 180) -> str:
    if not text:
        return ""
    start = max(text.rfind("\n", 0, max(0, index)) + 1, 0)
    end = text.find("\n", max(0, index))
    if end < 0:
        end = len(text)
    return text[start:end].strip()[:limit]


def _exact_phrase(text: str, normalized_words: tuple[str, ...]) -> str:
    if not normalized_words:
        return ""
    pattern = r"\b" + r"\W+".join(re.escape(word) for word in normalized_words) + r"\b"
    match = re.search(pattern, text, re.I)
    return match.group(0) if match else ""


def _estimate_syllables(word: str) -> int:
    clean = re.sub(r"[^a-z]", "", word.casefold())
    if not clean:
        return 1
    groups = re.findall(r"[aeiouy]+", clean)
    count = len(groups)
    if clean.endswith("e") and count > 1 and not clean.endswith(("le", "ye")):
        count -= 1
    return max(1, count)


def _style_signal_visible(signal: str, text: str) -> bool:
    pattern = _STYLE_SIGNAL_PATTERNS.get(signal)
    if pattern is not None:
        return bool(pattern.search(text))
    return bool(re.search(rf"\b{re.escape(signal)}(?:s|ed|ing)?\b", text, re.I))


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


__all__ = [
    "WordLimits",
    "check_english_quality",
    "english_word_limits",
    "profile_adherence_score",
]
