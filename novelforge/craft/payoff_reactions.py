"""F5 爽点三段式（v22）：动作 → 在场者反应 → 收获重要性展示。

依据（docs/NovelForge/23 P0-5）：猫腻"'看对方怎么输'才是快感的来源"；阅文"剧情 = 动作 + 反应"；
起点编辑子良"目标—行动—收获—收获重要性展示"。

扩展点（见 craft/extensions.py）：
- ``craft_checks``：爽点章（或下一章）至少 1 个非主角的可见反应，否则 warn。
- ``enrich_actuals``：从正文抽取契约 payoff beat 在场者的反应证据（ReactionEvidence）。
- ``draft_brief``：三段式提醒（点名在场者）+ 上一章欠下的余波 + 用 knowledge_edges
  推荐"信息差打脸"机会给规划/起草。
- ``settle``：只读汇总本章反应覆盖率（payoff_beats / reactions / witnesses_missing）。

确定性：反应检测是纯函数（词表 + 句/分句结构归属），不调 LLM，同一输入同一输出。
``craft_checks`` 的 ctx 不带运行配置：优先读 ``ctx.extra["cfg"]``，否则读
``draft_brief`` 抄进同章黑板的 ``workspace["payoff_reactions_cfg"]``，再否则用
``PayoffConfig()`` 默认值（见 ``_ctx_settings``）。
"欠余波"判定会重读窗口内前几章已提交的 L0 正文再跑检测器（只读）：非爽点章的
actuals 不记录反应，单看 actuals 永远无法了结上一爽点章的义务。
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from ..planning.actuals import find_evidence_span
from ..planning.contract_fields import normalize_witnesses
from ..planning.models import EvidenceSpan, ReactionEvidence

# ── 词表 ──────────────────────────────────────────────────────────────────────
# (正则, 态度类别)。类别写入 ReactionEvidence.attitude_shift，是稳定的枚举键。

_ZH_CUES: tuple[tuple[str, str], ...] = (
    # 震惊 / 失神
    (r"倒(?:吸|抽)(?:了)?(?:一?口)?(?:冷气|凉气)|倒(?:吸|抽)(?:了)?一口气", "shock"),
    (r"目瞪口呆|瞠目结舌|呆若木鸡|张大了嘴(?:巴)?", "shock"),
    (r"不敢置信|难以置信|不可置信|不敢相信|无法相信|不可思议", "shock"),
    (r"震惊|吃惊|大惊|惊愕|愕然|错愕|骇然|惊骇|惊呆|大骇|震撼", "shock"),
    (r"愣住|愣在|一愣|愣了|呆住|呆立|怔住|一怔|怔在|傻眼|(?:傻|懵)(?:住)?了|人都傻了", "shock"),
    (r"瞳孔(?:猛地|骤然)?(?:一缩|骤缩|猛缩|收缩|紧缩)", "shock"),
    (r"惊呼|惊叫|失声|哗然|心头一震|心中一震|浑身一震", "shock"),
    (r"(?:心中|心头|心里|心下)(?:一凛|一紧|一沉|大骇|暗惊|一突|巨震|大震|狂震)"
     r"|暗自心惊|心惊肉跳|如遭雷击|面面相觑|议论纷纷|交头接耳", "shock"),
    (r"瞪大(?:了)?(?:眼睛|双眼|眼)|双眼圆睁|眼珠子(?:都)?(?:快)?(?:要)?(?:瞪|掉)", "shock"),
    (r"僵在(?:了)?(?:原地|那里|当场)|(?:身子|身体|浑身)一僵|笑容(?:一僵|僵住|僵在|凝固|消失)"
     r"|嘴角(?:抽搐|抽动|一抽)|眼中闪过一丝(?:忌惮|惧意|惊骇|惊恐|慌乱)", "shock"),
    # 惧 / 色变（"脸色变了""脸色顿时变得难看""面色一白"等都算）
    (r"(?:脸色|面色|神色)(?:顿时|瞬间|骤然|立刻|陡然|当即|一下子|猛地)?"
     r"(?:大变|骤变|剧变|一变|狂变|微变|变了|一僵|一白|一青|煞白|惨白|发白|苍白|铁青|难看"
     r"|阴晴不定|青一阵白一阵|变得(?:极为|十分|异常|无比|很|极其)?(?:难看|苍白|煞白|惨白|铁青))",
     "fear"),
    (r"变了脸色|面如死灰|面无人色|冷汗|头皮发麻|双腿发软|腿一软|双膝一软", "fear"),
    (r"两股战战|瘫(?:软|坐)在地|浑身冰凉|吓了一(?:大)?跳|吓得"
     r"|(?:手|双手|身子|身体|双腿|嘴唇)(?:都|在|不停地|不住地)?(?:微微)?(?:发抖|颤抖|哆嗦)", "fear"),
    # 后退须带"受惊"限定：单纯的"后退一步"多是招式身法，不算反应。
    (r"连退|连连后退|踉跄(?:着)?(?:后退|倒退)|(?:吓|惊)得(?:连连)?(?:后退|倒退)"
     r"|(?:后|倒)?退了(?:一|两|三|几|半|数)步|倒退(?:一|两|三|几|半|数)步", "fear"),
    (r"浑身(?:发抖|颤抖|一颤|发颤)|惊恐|恐惧|畏惧", "fear"),
    (r"(?:咽|吞)了(?:一)?口唾沫", "fear"),
    # 服 / 跪 / 改口。"跪"须带结果（排除"跪坐"等姿态描写）；拱手/抱拳/躬身/行礼是
    # 日常礼节，只有接认输/赔罪/求饶等才算服软。
    (r"跪(?:下|倒|地|伏|拜|了|在地)", "submission"),
    (r"(?:拱手|抱拳|躬身|行礼)(?:认输|赔罪|告罪|求饶|致歉|认错|请罪)", "submission"),
    (r"(?:低|垂)下(?:了)?头|赔笑|赔罪|道歉|认输|心服口服|(?<![说制征降克])服了|改口",
     "submission"),
    # 敬畏 / 叫好
    (r"敬畏|肃然起敬|刮目相看|佩服|叹服|折服|崇拜|欢呼|喝彩|叫好|沸腾", "awe"),
    # 噤声
    (r"噤声|噤若寒蝉|鸦雀无声|一片死寂|死一般的(?:寂静|安静)|寂静无声", "silence"),
    (r"说不出话|哑口无言|无言以对|屏住(?:了)?呼吸", "silence"),
    (r"哑然|语塞|失语|半晌无言|(?:沉默|默然)(?:了|下来|不语)|一片寂静|落针可闻", "silence"),
    # 悔 / 恨
    (r"后悔|悔不当初|懊悔|悔恨|悔青", "regret"),
    (r"咬牙|咬紧(?:了)?牙关|牙关紧咬|(?:攥|握)紧(?:了)?(?:拳头|双拳)|怨毒|恨恨|不甘", "resentment"),
    (r"(?:脸色|面色)阴沉", "resentment"),
)

_EN_CUES: tuple[tuple[str, str], ...] = (
    (r"gasp(?:ed|s|ing)?|stunned|shocked|astonish(?:ed|ment)|aghast|dumbstruck|thunderstruck",
     "shock"),
    (r"speechless|disbelief|(?:could|can)(?:\s*not|n['’]t)\s+believe", "shock"),
    (r"(?:jaws?|mouths?)\s+(?:dropped|fell open|hung open|fell slack)", "shock"),
    (r"eyes\s+(?:widened|went wide|bulged)", "shock"),
    (r"(?:face|expression|features)\s+(?:went|fell)\s+slack|slack-jawed", "shock"),
    (r"stiffened|went (?:rigid|stiff|still)|(?:breath|breathing)\s+(?:caught|hitched)", "shock"),
    (r"(?:grin|smile|smirk|sneer)s?\s+(?:vanished|faded|froze|died|slipped|disappeared"
     r"|fell away)", "shock"),
    (r"sputtered|spluttered|stammered|stuttered", "shock"),
    # 单独的 murmured 多是说话动词（"Elder Wang murmured, ..."），只认群像或带情绪的。
    (r"(?<=crowd )murmured|(?<=onlookers )murmured|(?<=spectators )murmured"
     r"|(?<=audience )murmured|murmured in (?:surprise|astonishment|disbelief|shock|awe)"
     r"|whispers?\s+(?:broke out|spread|rippled|ran)"
     r"|murmurs?\s+(?:ran|rippled|spread|broke out|rose)", "shock"),
    # A bare "stared" is a stage direction; only a dumbstruck stare counts.
    (r"froze|frozen in place|gaped|gawked|did a double take"
     r"|star(?:ed|ing)\s+(?:dumbly|blankly|wide-eyed|open-mouthed"
     r"|in (?:disbelief|shock|horror|astonishment))", "shock"),
    (r"erupted (?:in(?:to)? )?(?:an )?(?:uproar|gasps|exclamations)|(?:in|into) an uproar", "shock"),
    (r"(?:went|turned|grew)\s+(?:pale|white|ashen|gr[ae]y|bloodless)|paled|blanched|ashen", "fear"),
    (r"flinch(?:ed|es)?|trembl(?:ed|ing)|shudder(?:ed|ing)|recoiled", "fear"),
    (r"(?:colou?r|blood)\s+drain(?:ed|ing|s)?\s+from|drained of colou?r", "fear"),
    (r"(?:hands?|fingers|knees|legs)\s+(?:shook|were shaking|began to shake|went weak)", "fear"),
    (r"swallowed (?:nervously|dryly|thickly|convulsively)"
     r"|took an? (?:involuntary|half|hasty|startled|nervous|unsteady) step back(?:ward)?", "fear"),
    # Stepping back is often just footwork; it needs a scared qualifier.
    (r"backed away|stumbled back(?:ward)?|staggered back(?:ward)?|swallowed hard|cold sweat"
     r"|(?:stepped|took a step) back (?:involuntarily|in (?:fear|shock|alarm|horror))", "fear"),
    (r"broke (?:out )?in(?:to)? a (?:cold )?sweat", "fear"),
    (r"knelt|kneel(?:s|ing)?|(?:dropped|fell|sank) to (?:his|her|their|one) knees?", "submission"),
    # A polite bow is etiquette: only a bowed head or a bow of defeat counts.
    (r"bowed (?:his|her|their) heads?"
     r"|bow(?:ed|ing) (?:low|deeply|apologetically|in (?:defeat|submission|apology))"
     r"|lowered (?:his|her|their) (?:heads?|eyes|gaze)", "submission"),
    (r"apologi[sz](?:ed|es|ing)|conceded|yielded", "submission"),
    (r"in awe|awed|awestruck|reverence", "awe"),
    (r"cheered|cheers (?:erupted|rang out|went up)|erupted (?:in(?:to)? )?(?:cheers|applause)"
     r"|applaud(?:ed)?|applause", "awe"),
    (r"fell silent|went silent|(?:fell|went) quiet|(?:fell|went|grew) hushed|a hush fell"
     r"|silence fell", "silence"),
    (r"a murmur (?:ran|spread|rippled)", "silence"),
    (r"regret(?:ted|ting|s)?|took back (?:his|her|their) words|changed (?:his|her|their) tune"
     r"|retracted", "regret"),
    (r"gritted (?:his|her|their) teeth|clenched (?:his|her|their) (?:jaw|fists?)"
     r"|(?:face|expression) darkened|scowled|glared", "resentment"),
    (r"jaws?\s+(?:tightened|clenched|set)|eyes\s+narrowed|narrowed (?:his|her|their) eyes",
     "resentment"),
)

# 通用群像反应者（不需要在 witnesses 里点名）。
CROWD_REACTORS_ZH: tuple[str, ...] = (
    "在场众人", "围观众人", "众人", "全场", "满场", "围观者", "围观的人", "所有人",
    "其他人", "人群", "众弟子", "众长老", "四座", "满堂",
)
CROWD_REACTORS_EN: tuple[str, ...] = (
    "everyone", "everybody", "the crowd", "the hall", "the room", "the onlookers",
    "the spectators", "the audience", "the courtyard", "the whole hall", "the whole room",
    "onlookers", "spectators",
)
_CROWD_SET = frozenset(CROWD_REACTORS_ZH) | frozenset(CROWD_REACTORS_EN)

_ZH_PRONOUN_RE = re.compile(r"他们|她们|(?<!其)他|她")
_EN_PRONOUN_RE = re.compile(r"\b(?:he|she|they|his|her|their)\b", re.IGNORECASE)
# "对方"/"his opponent"：从主角视角指对手——只会指向非主角。
_ZH_OPPONENT_RE = re.compile(r"对方|对手")
_EN_OPPONENT_RE = re.compile(
    r"\b(?:his|her|their|the) (?:opponent|rival|enemy|adversary)\b"
    r"|\bthe other (?:man|woman|youth|boy|girl)\b",
    re.IGNORECASE,
)
# 主角作主语时宾语前的介词（"林凡向王长老道歉"）：其后的 cue 仍是主角的动作。
_ZH_DIRECTION_RE = re.compile(r"(?:向|朝|对|冲|给|替)着?$")
_EN_DIRECTION_RE = re.compile(r"\b(?:to|toward|towards|at)$", re.IGNORECASE)
# cue 在前、群像在后（"A murmur ran through the crowd" / "惊呼声响彻全场"）：两者之间只允许
# 这类"传遍/穿过"的连接，群像才算反应者。
_EN_CROWD_GAP_RE = re.compile(
    r"(?:(?:ran|rippled|spread|swept|went up|broke out|rose|erupted|fell)\s+)?"
    r"(?:through|across|over|among|from|in)",
    re.IGNORECASE,
)
_ZH_CROWD_GAP_RE = re.compile(r"声?(?:在|从|响彻|传遍)|了")
# 紧邻 cue 之前的否定（"没有丝毫震惊""毫不畏惧" / "did not flinch""never gasped"）：
# 不是反应，而是刻意的"不为所动"。"从未如此震惊""忍不住倒吸一口凉气"不算否定。
_ZH_NEGATED_RE = re.compile(
    r"(?:没有|并未|并无|并不|毫不|毫无|不曾|未曾|从不|没|未|不)"
    r"(?:丝毫|一丝|半点|任何|一点|半分)?(?:的)?$"
)
# 双重否定是强调肯定（"众人无不骇然""莫不脸色大变""不得不低下头""谁不震惊"），不算否定。
_ZH_DOUBLE_NEGATION_RE = re.compile(
    r"(?:无|莫|无一|无人|没人|没有|没有人|没有一个人?|谁|哪个|不得|不能)不$"
)
_EN_NEGATED_RE = re.compile(
    r"(?:\b(?:not|never|no|without)|n['’]t)(?:\s+(?:even|once|visibly|so much as))?\s*$",
    re.IGNORECASE,
)
_NEGATION_WINDOW_SPLIT_RE = re.compile(r"[，,；;：:。！？!?.\n]|——|—|--")
_ZH_COORD_RE = re.compile(r"\s*(?:、|和|与|及|以及|跟|同)\s*")
_EN_COORD_RE = re.compile(r"\s*(?:and|&|as well as)\s*", re.IGNORECASE)
_ZH_CLAUSE_SEP_RE = re.compile(r"[，,；;：:]|——")
_EN_CLAUSE_SEP_RE = re.compile(r"[,;:]|—|--")
_ZH_SENTENCE_RE = re.compile(r"[^。！？!?]+[。！？!?]*[”」』\"']*")
_EN_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]*[\"'”’)]*")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
_QUOTE_PAIRS = {"“": "”", "「": "」", "『": "』", '"': '"'}
_EN_TITLES = frozenset({
    "elder", "lord", "lady", "master", "sir", "mr", "mrs", "ms", "miss", "dr", "doctor",
    "captain", "king", "queen", "prince", "princess", "young", "old", "brother", "sister",
    "uncle", "aunt", "grand", "great", "saint", "senior", "junior", "the",
})

_ZH_CUE_RES = tuple((re.compile(p), a) for p, a in _ZH_CUES)
_EN_CUE_RES = tuple(
    (re.compile(rf"(?<![A-Za-z])(?:{p})(?![A-Za-z])", re.IGNORECASE), a) for p, a in _EN_CUES
)

_KIND_RANK = {"protagonist": 0, "witness": 1, "crowd": 2}
_MAX_EVIDENCE_CHARS = 160
_CFG_KEY = "payoff_reactions_cfg"


def is_crowd_reactor(name: str) -> bool:
    """True for the generic crowd reactors (众人 / the crowd …)."""
    value = str(name or "").strip()
    return value in _CROWD_SET or value.lower() in _CROWD_SET


def is_english(language: Optional[str]) -> bool:
    return str(language or "").strip().lower().replace("_", "-").startswith("en")


def infer_language(text: str) -> str:
    """Fallback language guess from the draft: CJK-dominant → zh-CN, else en-US."""
    sample = (text or "")[:4000]
    cjk = len(_CJK_RE.findall(sample))
    latin = len(_LATIN_RE.findall(sample))
    if cjk and cjk * 3 >= latin:
        return "zh-CN"
    return "en-US" if latin else "zh-CN"


# ── 纯函数检测器 ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Hit:
    start: int
    end: int
    label: str          # 反应者标签（witness 原名 / 群像词）或 cue 原文
    kind: str           # protagonist | witness | crowd | cue
    attitude: str = ""


@dataclass
class _Sentence:
    start: int
    end: int
    paragraph: int
    clauses: list[tuple[int, int]] = field(default_factory=list)


def _quote_mask(text: str) -> bytearray:
    """Mark quoted speech (“…” 「…」 『…』 "…"); an unclosed quote ends at the line."""
    mask = bytearray(len(text))
    closer: Optional[str] = None
    opened = 0
    for idx, char in enumerate(text):
        if closer is None:
            if char in _QUOTE_PAIRS:
                closer, opened = _QUOTE_PAIRS[char], idx
        elif char == closer or char == "\n":
            mask[opened:idx + 1] = b"\x01" * (idx + 1 - opened)
            closer = None
    if closer is not None:
        mask[opened:] = b"\x01" * (len(text) - opened)
    return mask


def _segments(text: str, en: bool) -> list[_Sentence]:
    sentence_re = _EN_SENTENCE_RE if en else _ZH_SENTENCE_RE
    clause_re = _EN_CLAUSE_SEP_RE if en else _ZH_CLAUSE_SEP_RE
    out: list[_Sentence] = []
    offset = 0
    for paragraph, line in enumerate(text.split("\n")):
        for match in sentence_re.finditer(line):
            raw = match.group(0)
            stripped = raw.strip()
            if not stripped:
                continue
            start = offset + match.start() + (len(raw) - len(raw.lstrip()))
            end = start + len(stripped)
            sentence = _Sentence(start, end, paragraph)
            cursor = start
            for sep in clause_re.finditer(text, start, end):
                sentence.clauses.append((cursor, sep.start()))
                cursor = sep.end()
            sentence.clauses.append((cursor, end))
            out.append(sentence)
        offset += len(line) + 1
    return out


def _name_pattern(name: str, *, ignore_case: bool) -> re.Pattern[str]:
    escaped = re.escape(name)
    if _LATIN_RE.search(name):
        escaped = rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
    return re.compile(escaped, re.IGNORECASE if ignore_case else 0)


def _clean_name(raw: Any) -> str:
    return _PAREN_RE.sub("", str(raw or "")).strip()


def _usable_name(name: str) -> bool:
    return len(name) >= 2


def _en_variants(name: str) -> list[str]:
    """First/last tokens of an English multi-word name, skipping honorifics."""
    tokens = [tok for tok in re.split(r"\s+", name) if tok]
    if len(tokens) < 2 or not _LATIN_RE.search(name):
        return []
    out: list[str] = []
    for tok in (tokens[0], tokens[-1]):
        bare = tok.strip(".,'’")
        if len(bare) >= 3 and bare[:1].isupper() and bare.lower() not in _EN_TITLES:
            if bare not in out:
                out.append(bare)
    return out


def _resolve_overlaps(hits: Iterable[_Hit]) -> list[_Hit]:
    """Leftmost-longest, non-overlapping (protagonist > witness > crowd on ties)."""
    kept: list[_Hit] = []
    last_end = -1
    for hit in sorted(hits, key=lambda h: (h.start, -(h.end - h.start), _KIND_RANK.get(h.kind, 3))):
        if hit.start >= last_end:
            kept.append(hit)
            last_end = hit.end
    return kept


def _find_mentions(
    text: str,
    mask: bytearray,
    surfaces: Sequence[tuple[str, str, str]],
) -> list[_Hit]:
    """``surfaces`` = [(surface text, reactor label, kind)] → unquoted mentions."""
    hits: list[_Hit] = []
    for surface, label, kind in surfaces:
        pattern = _name_pattern(surface, ignore_case=kind == "crowd")
        for match in pattern.finditer(text):
            if not mask[match.start()]:
                hits.append(_Hit(match.start(), match.end(), label, kind))
    return _resolve_overlaps(hits)


def _negated(text: str, pos: int, en: bool) -> bool:
    """A negator right before the cue, inside the same clause ("没有丝毫震惊" / "did not flinch")."""
    window = _NEGATION_WINDOW_SPLIT_RE.split(text[max(0, pos - (24 if en else 8)):pos])[-1]
    if en:
        return bool(_EN_NEGATED_RE.search(window))
    return not _ZH_DOUBLE_NEGATION_RE.search(window) and bool(_ZH_NEGATED_RE.search(window))


def _find_cues(text: str, mask: bytearray, en: bool) -> list[_Hit]:
    hits: list[_Hit] = []
    for pattern, attitude in (_EN_CUE_RES if en else _ZH_CUE_RES):
        for match in pattern.finditer(text):
            if not mask[match.start()]:
                hits.append(_Hit(match.start(), match.end(), match.group(0), "cue", attitude))
    return [hit for hit in _resolve_overlaps(hits) if not _negated(text, hit.start, en)]


def _coordinated(text: str, left: _Hit, right: _Hit, en: bool) -> bool:
    gap = text[left.end:right.start]
    return bool((_EN_COORD_RE if en else _ZH_COORD_RE).fullmatch(gap))


def _group_around(text: str, mentions: list[_Hit], idx: int, en: bool, *, forward: bool) -> list[_Hit]:
    """Coordinated mentions ("王长老、赵师兄" / "Wang and Li") starting/ending at ``idx``."""
    group = [mentions[idx]]
    step = 1 if forward else -1
    j = idx
    while 0 <= j + step < len(mentions):
        nxt = j + step
        left, right = (mentions[j], mentions[nxt]) if forward else (mentions[nxt], mentions[j])
        if not _coordinated(text, left, right, en):
            break
        j = nxt
        group.append(mentions[j])
    return sorted(group, key=lambda h: h.start)


def _near_clause_start(text: str, clause_start: int, pos: int, en: bool) -> bool:
    prefix = text[clause_start:pos].strip()
    if en:
        return len(prefix.split()) <= 2
    return len(prefix) <= 4


def _non_protagonist(group: Iterable[_Hit]) -> list[_Hit]:
    return [h for h in group if h.kind != "protagonist"]


def _clause_of(sentence: _Sentence, pos: int) -> tuple[int, int]:
    return next((c for c in sentence.clauses if c[0] <= pos < c[1]), sentence.clauses[-1])


def _subject_of(
    text: str, mentions: list[_Hit], sentence: _Sentence, en: bool,
) -> list[_Hit]:
    """Subject group of a sentence whose first mention opens its clause; [] otherwise."""
    if not mentions:
        return []
    head = mentions[0]
    clause = _clause_of(sentence, head.start)
    if head.kind == "protagonist" or not _near_clause_start(text, clause[0], head.start, en):
        return []
    return _non_protagonist(_group_around(text, mentions, 0, en, forward=True))


def _attribute(
    text: str,
    cue: _Hit,
    sentence: _Sentence,
    sentence_mentions: list[_Hit],
    history: list[tuple[_Sentence, list[_Hit]]],
    en: bool,
) -> tuple[list[_Hit], float, tuple[int, int]]:
    """Who reacts with ``cue``?  Returns (reactors, confidence, cue clause).

    ``history`` holds up to two preceding sentences of the same paragraph
    (nearest first) with their mentions.
    """
    clause = _clause_of(sentence, cue.start)
    in_clause = [m for m in sentence_mentions if clause[0] <= m.start and m.end <= clause[1]]
    before = [m for m in in_clause if m.end <= cue.start]

    # A. 同分句、在 cue 之前：主语（首个提及 + 并列）；主角作主语时取最近的非主角提及
    #    （"林凡看着王长老脸色大变"）——但宾语前是"向/朝/对…"时动作仍属主角
    #    （"林凡向王长老道歉""林凡朝赵师兄咬牙道"），不归给宾语。
    if before:
        if before[0].kind != "protagonist":
            group = _non_protagonist(_group_around(text, before, 0, en, forward=True))
            return group, (0.8 if all(h.kind == "crowd" for h in group) else 0.9), clause
        others = [i for i, m in enumerate(before) if m.kind != "protagonist"]
        if not others:
            return [], 0.0, clause
        group = _group_around(text, before, others[-1], en, forward=False)
        lead_start = max((m.end for m in before if m.end <= group[0].start), default=clause[0])
        lead = text[lead_start:group[0].start].strip()
        if (_EN_DIRECTION_RE if en else _ZH_DIRECTION_RE).search(lead):
            return [], 0.0, clause
        return _non_protagonist(group), 0.7, clause

    # B. 定语式："脸色大变的王长老" / "a stunned Elder Wang"；
    #    群像在 cue 之后："A murmur ran through the crowd" / "惊呼声响彻全场"。
    after = [m for m in in_clause if m.start >= cue.end]
    if after:
        gap = text[cue.end:after[0].start].strip()
        if (en and gap == "") or (not en and gap == "的"):
            return _non_protagonist([after[0]]), 0.8, clause
        if after[0].kind == "crowd" and (_EN_CROWD_GAP_RE if en else _ZH_CROWD_GAP_RE).fullmatch(gap):
            return [after[0]], 0.7, clause

    # C. "对方面如死灰"：对手指代只会指向非主角——本句 cue 前、否则同段前两句里最近的一个。
    if (_EN_OPPONENT_RE if en else _ZH_OPPONENT_RE).search(text, clause[0], cue.start):
        pools = [[m for m in sentence_mentions if m.end <= cue.start]]
        pools += [mentions for _, mentions in history]
        for pool in pools:
            others = _non_protagonist(pool)
            if others:
                return others[-1:], 0.6, clause
        return [], 0.0, clause

    # D. 承前省略主语："王长老看着林凡，脸色大变"。
    for prev_clause in reversed(sentence.clauses[:sentence.clauses.index(clause)]):
        prev_mentions = [
            m for m in sentence_mentions if prev_clause[0] <= m.start and m.end <= prev_clause[1]
        ]
        if prev_mentions:
            return _subject_of(text, prev_mentions, sentence, en), 0.75, clause

    # E. 代词承接上一句（同段）："王长老盯着碎玉。他脸色大变。"
    pronoun = (_EN_PRONOUN_RE if en else _ZH_PRONOUN_RE).search(text, sentence.start, cue.start)
    if pronoun is None or not history:
        return [], 0.0, clause
    previous, previous_mentions = history[0]
    return _subject_of(text, previous_mentions, previous, en), 0.6, clause


def _bucket(hits: list[_Hit], sentences: list[_Sentence], *, whole: bool) -> dict[int, list[_Hit]]:
    """Group start-sorted hits by sentence index (``whole``: hit must fit inside)."""
    out: dict[int, list[_Hit]] = {}
    idx = 0
    for hit in hits:
        while idx < len(sentences) and sentences[idx].end <= hit.start:
            idx += 1
        if idx >= len(sentences) or sentences[idx].start > hit.start:
            continue
        if whole and hit.end > sentences[idx].end:
            continue
        out.setdefault(idx, []).append(hit)
    return out


def detect_reactions(
    draft_text: str,
    witnesses: Sequence[str],
    *,
    language: Optional[str] = None,
    protagonist_names: Sequence[str] = (),
    aliases: Optional[Mapping[str, Sequence[str]]] = None,
    beat_seq: Optional[int] = None,
    target: Optional[str] = None,
    max_per_reactor: int = 3,
    max_total: int = 30,
) -> list[ReactionEvidence]:
    """Find visible non-protagonist reactions in ``draft_text``.

    A reaction is a reaction cue (震惊/脸色大变/倒吸一口凉气… · gasped/paled/knelt…)
    attributed to a witness (or a generic crowd reactor) by sentence structure:
    the clause subject before the cue, an attributive "…的王长老", "对方" (the
    protagonist's opponent), the subject carried over from an earlier clause, or a
    pronoun pointing back one sentence.
    Quoted speech is ignored (taunts like “你后悔了吗” are not reactions) and the
    protagonist is never a reactor.  Evidence offsets are relative to ``draft_text``.
    """
    text = draft_text or ""
    if not text.strip():
        return []
    en = is_english(language or infer_language(text))

    protagonist: list[str] = []
    for raw in protagonist_names or ():
        name = _clean_name(raw)
        if _usable_name(name):
            protagonist.append(name)
            protagonist.extend(_en_variants(name))
    surfaces: list[tuple[str, str, str]] = [(name, name, "protagonist") for name in protagonist]
    seen = set(protagonist)
    for raw in witnesses or ():
        label = _clean_name(raw)
        if not _usable_name(label) or label in seen:
            continue
        extra = (aliases or {}).get(label) or (aliases or {}).get(raw) or ()
        names = [label, *(_clean_name(a) for a in extra)]
        for name in [*names, *_en_variants(label)]:
            if _usable_name(name) and name not in seen:
                seen.add(name)
                surfaces.append((name, label, "witness"))
    for term in (CROWD_REACTORS_EN if en else CROWD_REACTORS_ZH):
        if term not in seen:
            surfaces.append((term, term, "crowd"))

    mask = _quote_mask(text)
    cues = _find_cues(text, mask, en)
    if not cues:
        return []
    sentences = _segments(text, en)
    mentions = _bucket(_find_mentions(text, mask, surfaces), sentences, whole=True)
    cues_by_sentence = _bucket(cues, sentences, whole=False)

    out: list[ReactionEvidence] = []
    per_reactor: dict[str, int] = {}
    for idx in sorted(cues_by_sentence):
        sentence = sentences[idx]
        history = [
            (sentences[j], mentions.get(j, []))
            for j in (idx - 1, idx - 2)
            if j >= 0 and sentences[j].paragraph == sentence.paragraph
        ]
        credited: set[str] = set()
        for cue in cues_by_sentence[idx]:
            reactors, confidence, clause = _attribute(
                text, cue, sentence, mentions.get(idx, []), history, en,
            )
            for reactor in reactors:
                label = reactor.label
                if label in credited or per_reactor.get(label, 0) >= max_per_reactor:
                    continue
                credited.add(label)
                per_reactor[label] = per_reactor.get(label, 0) + 1
                start, end = sentence.start, sentence.end
                if end - start > _MAX_EVIDENCE_CHARS:
                    anchor = reactor.start if start <= reactor.start < end else clause[0]
                    start, end = min(anchor, clause[0]), clause[1]
                out.append(ReactionEvidence(
                    reactor=label,
                    target=target or None,
                    reaction=cue.label,
                    attitude_shift=cue.attitude or None,
                    beat_seq=beat_seq if beat_seq and beat_seq >= 1 else None,
                    evidence_spans=[EvidenceSpan(
                        start_char=start, end_char=end, text=text[start:end], label="reaction",
                    )],
                    confidence=confidence,
                ))
                if len(out) >= max_total:
                    return out
    return out


# ── 契约 / beats 辅助 ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PayoffBeat:
    seq: int
    summary: str
    witnesses: tuple[str, ...]
    # 打脸/羞辱/当众…：本拍写明需要观众（见 _needs_audience）
    audience: bool = False


# 本拍文字写明"要有人看着"的爽点（打脸/羞辱/当众…，与 payoff_binding 的在场者要求同口径）。
# 未点名在场者、也不属于此类的爽点（闭关突破、独自获宝）本章可能本就无人在场。
_AUDIENCE_WORDS: tuple[str, ...] = (
    "打脸", "羞辱", "当众", "众目睽睽", "众人面前", "face_slap", "face-slap", "face slap",
    "humiliat", "comeuppance", "turn the tables", "turns the tables", "in public",
    "publicly", "in front of",
)


def _needs_audience(beat: Mapping[str, Any]) -> bool:
    text = f"{beat.get('summary') or ''} {beat.get('value_axis') or ''}".lower()
    return any(word in text for word in _AUDIENCE_WORDS)


def _witness_list(raw: Any) -> list[str]:
    out: list[str] = []
    for name in normalize_witnesses(raw):
        cleaned = _clean_name(name)
        if _usable_name(cleaned) and cleaned not in out:
            out.append(cleaned)
    return out


def payoff_beats(beats: Any) -> list[PayoffBeat]:
    """``payoff_beat`` entries of a beat list (planner dicts or contract rows)."""
    out: list[PayoffBeat] = []
    if not isinstance(beats, (list, tuple)):
        return out
    for index, beat in enumerate(beats, 1):
        if not isinstance(beat, Mapping) or beat.get("beat_type") != "payoff_beat":
            continue
        try:
            seq = int(beat.get("seq") or index)
        except (TypeError, ValueError):
            seq = index
        out.append(PayoffBeat(
            seq=max(1, seq),
            summary=str(beat.get("summary") or "").strip(),
            witnesses=tuple(_witness_list(beat.get("witnesses"))),
            audience=_needs_audience(beat),
        ))
    return out


def _union_witnesses(beats: Sequence[PayoffBeat], exclude: Iterable[str] = ()) -> list[str]:
    excluded = set(exclude)
    out: list[str] = []
    for beat in beats:
        for name in beat.witnesses:
            if name not in out and name not in excluded:
                out.append(name)
    return out


def _context_dict(plan_context: Any) -> dict[str, Any]:
    if plan_context is None:
        return {}
    if isinstance(plan_context, Mapping):
        return dict(plan_context)
    to_dict = getattr(plan_context, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        return value if isinstance(value, dict) else {}
    return {}


def _contract_beats(plan_context: Any) -> list[Any]:
    contract = _context_dict(plan_context).get("current_contract") or {}
    beats = contract.get("beats") if isinstance(contract, Mapping) else None
    return list(beats or [])


def _workspace_beats(workspace: Mapping[str, Any]) -> list[Any]:
    """Planned beats of the chapter: the drafted beat sheet, else the contract's."""
    beats = workspace.get("beats")
    if isinstance(beats, (list, tuple)) and beats:
        return list(beats)
    return _contract_beats(workspace.get("plan_context"))


def detect_for_payoff_beats(
    draft_text: str,
    beats: Sequence[PayoffBeat],
    *,
    language: Optional[str],
    protagonist_names: Sequence[str] = (),
    aliases: Optional[Mapping[str, Sequence[str]]] = None,
) -> list[ReactionEvidence]:
    """Run the detector once with every payoff witness and bind each hit to a beat.

    Witness reactions belong to the beat listing that witness; crowd reactions to
    the payoff beat whose text anchor last precedes them (else the first payoff beat).
    """
    if not beats:
        return []
    protagonist = {_clean_name(n) for n in protagonist_names}
    found = detect_reactions(
        draft_text, _union_witnesses(beats, protagonist), language=language,
        protagonist_names=protagonist_names, aliases=aliases,
    )
    if not found:
        return []
    anchors: dict[int, int] = {}
    if len(beats) > 1:
        for beat in beats:
            span = find_evidence_span(draft_text, beat.summary) if beat.summary else None
            if span is not None:
                anchors[beat.seq] = span.start_char
    out: list[ReactionEvidence] = []
    for reaction in found:
        owners = [b for b in beats if reaction.reactor in b.witnesses] or list(beats)
        position = reaction.evidence_spans[0].start_char if reaction.evidence_spans else 0
        positioned = [b for b in owners if b.seq in anchors and anchors[b.seq] <= position]
        chosen = max(positioned, key=lambda b: anchors[b.seq]) if positioned else owners[0]
        out.append(reaction.model_copy(update={
            "beat_seq": chosen.seq,
            "target": chosen.summary[:120] or None,
        }))
    return out


# ── 配置 ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Settings:
    enabled: bool
    window: int
    info_gaps: int


def _bounded(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _settings(source: Any) -> _Settings:
    """From a ``NovelForgeConfig`` (``.payoff``) or the workspace copy of it."""
    from ..config import PayoffConfig

    default = PayoffConfig()
    keys = ("enabled", "reaction_window_chapters", "info_gap_suggestions")
    if isinstance(source, Mapping):
        raw = source.get(_CFG_KEY)
        values = {key: raw.get(key) for key in keys} if isinstance(raw, Mapping) else {}
    else:
        payoff = getattr(source, "payoff", None) or default
        values = {key: getattr(payoff, key, None) for key in keys}
    enabled = values.get("enabled")
    return _Settings(
        enabled=default.enabled if enabled is None else bool(enabled),
        window=_bounded(values.get("reaction_window_chapters"),
                        default.reaction_window_chapters, 0, 10),
        info_gaps=_bounded(values.get("info_gap_suggestions"),
                           default.info_gap_suggestions, 0, 10),
    )


def _ctx_settings(workspace: Mapping[str, Any], extra: Any) -> _Settings:
    """Settings for ``craft_checks`` (whose ``ctx`` carries no config object).

    Order: a runtime config passed explicitly as ``ctx.extra["cfg"]`` /
    ``workspace["cfg"]`` → the copy ``draft_brief`` put on the same chapter's
    workspace (orchestrator pipeline and its ``validate_final_candidate`` copy)
    → ``PayoffConfig()`` defaults.  The API manual candidate swap and the
    governance ``/check/craft`` endpoint carry neither and use the defaults —
    the same values their own ``build_project_config`` would yield, because
    ``cfg.payoff`` has no project-level setting.
    """
    for holder in (extra, workspace):
        if isinstance(holder, Mapping):
            cfg = holder.get("cfg")
            if cfg is not None and getattr(cfg, "payoff", None) is not None:
                return _settings(cfg)
    return _settings(workspace)


# ── 数据库读取（全部只读、容忍老库）────────────────────────────────────────────

def _query(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    if conn is None:
        return []
    try:
        return list(conn.execute(sql, tuple(params)).fetchall())
    except sqlite3.Error:
        return []


def _entity_names(conn: Any, entity_id: str) -> list[str]:
    names = [str(r[0]) for r in _query(conn, "SELECT canonical_name FROM entities WHERE id=?",
                                       (entity_id,))]
    names += [str(r[0]) for r in _query(
        conn, "SELECT alias FROM entity_aliases WHERE entity_id=? ORDER BY alias", (entity_id,),
    )]
    return [n for n in dict.fromkeys(_clean_name(n) for n in names) if _usable_name(n)]


def _resolve_entity_id(conn: Any, name: str) -> Optional[str]:
    name = _clean_name(name)
    if not name:
        return None
    for sql in (
        "SELECT id FROM entities WHERE id=? LIMIT 1",
        "SELECT id FROM entities WHERE canonical_name=? LIMIT 1",
        "SELECT entity_id FROM entity_aliases WHERE alias=? LIMIT 1",
    ):
        rows = _query(conn, sql, (name,))
        if rows:
            return str(rows[0][0])
    return None


def _find_name(text: str, name: str) -> int:
    """Index of ``name`` in ``text`` (word-bounded for Latin names), -1 when absent."""
    if not _LATIN_RE.search(name):
        return text.find(name)
    match = _name_pattern(name, ignore_case=False).search(text)
    return match.start() if match else -1


def _looks_like_name(text: str) -> bool:
    return 2 <= len(text) <= 24 and not re.search(r"[，。,.;；:：!！?？()（）\n]", text)


@dataclass(frozen=True)
class Protagonist:
    entity_id: Optional[str] = None
    names: tuple[str, ...] = ()


def _once(fn: Callable[[], Any]) -> Callable[[], Any]:
    """Compute on first use only (most chapters never need the protagonist/language)."""
    cache: list[Any] = []

    def get() -> Any:
        if not cache:
            cache.append(fn())
        return cache[0]
    return get


def resolve_protagonist(conn: Any, chapter: int) -> Protagonist:
    """``meta_kv['protagonist']`` (free-text setting → named character), else the POV card."""
    meta_rows = _query(conn, "SELECT value FROM meta_kv WHERE key='protagonist'")
    meta = str(meta_rows[0][0] or "").strip() if meta_rows else ""
    entity_id: Optional[str] = None
    if meta:
        candidates: list[tuple[int, int, str]] = []
        for row in _query(
            conn,
            "SELECT id, canonical_name FROM entities WHERE entity_type='character'"
            " UNION ALL SELECT a.entity_id, a.alias FROM entity_aliases a"
            " JOIN entities e ON e.id=a.entity_id WHERE e.entity_type='character'",
        ):
            name = _clean_name(row[1])
            pos = _find_name(meta, name) if _usable_name(name) else -1
            if pos >= 0:
                candidates.append((pos, -len(name), str(row[0])))
        if candidates:
            entity_id = min(candidates)[2]
    if entity_id is None:
        rows = _query(conn, "SELECT pov_entity_id FROM chapter_cards WHERE chapter=?", (chapter,))
        if rows and rows[0][0]:
            entity_id = str(rows[0][0])
    names = _entity_names(conn, entity_id) if entity_id else []
    if not names and meta and _looks_like_name(meta):
        names = [meta]
    return Protagonist(entity_id, tuple(names))


def _drop_protagonist(
    conn: Any, witnesses: Sequence[str], protagonist: Protagonist,
) -> tuple[list[str], dict[str, list[str]]]:
    """Remove witnesses that are the protagonist; collect entity aliases for the rest."""
    kept: list[str] = []
    aliases: dict[str, list[str]] = {}
    for name in witnesses:
        if name in protagonist.names:
            continue
        entity_id = _resolve_entity_id(conn, name)
        if entity_id and entity_id == protagonist.entity_id:
            continue
        kept.append(name)
        extra = [n for n in _entity_names(conn, entity_id) if n != name] if entity_id else []
        if extra:
            aliases[name] = extra
    return kept, aliases


def _is_protagonist(conn: Any, name: str, protagonist: Protagonist) -> bool:
    if not name or name in protagonist.names:
        return True
    if not protagonist.entity_id:
        return False
    return _resolve_entity_id(conn, name) == protagonist.entity_id


def _visible_reactors(
    conn: Any, reactions: Iterable[Any], protagonist: Protagonist,
) -> list[str]:
    """Reactors of stored/detected reactions minus the protagonist.

    ``enrich_actuals`` has no DB, so persisted actuals may credit the protagonist
    when the planner listed them as a witness; every reader filters here.
    """
    out: list[str] = []
    for item in reactions:
        raw = item.get("reactor") if isinstance(item, Mapping) else getattr(item, "reactor", "")
        name = _clean_name(raw)
        if name and not _is_protagonist(conn, name, protagonist):
            out.append(name)
    return out


def _restrict(beats: Sequence[PayoffBeat], kept: Sequence[str]) -> list[PayoffBeat]:
    allowed = set(kept)
    return [PayoffBeat(b.seq, b.summary, tuple(w for w in b.witnesses if w in allowed),
                       b.audience)
            for b in beats]


def _stored_contract_beats(conn: Any, chapter: int) -> list[dict[str, Any]]:
    rows = _query(conn, "SELECT current_plan_version_id FROM chapter_cards WHERE chapter=?", (chapter,))
    version_id = rows[0][0] if rows else None
    tails: list[tuple[str, tuple[Any, ...]]] = []
    if version_id:
        tails.append(("FROM chapter_plan_version_beats WHERE plan_version_id=? ORDER BY seq",
                      (version_id,)))
    tails.append(("FROM beats WHERE chapter=? AND status<>'dropped' ORDER BY seq", (chapter,)))
    for tail, params in tails:
        result = _query(conn, f"SELECT seq, beat_type, summary, witnesses_json {tail}", params)
        if not result:   # 无数据，或 v22 前老库没有 witnesses_json 列
            result = [(*row, None) for row in _query(conn, f"SELECT seq, beat_type, summary {tail}",
                                                     params)]
        if result:
            beats = []
            for row in result:
                try:
                    witnesses = json.loads(row[3]) if row[3] else []
                except (TypeError, ValueError):
                    witnesses = []
                beats.append({"seq": row[0], "beat_type": row[1], "summary": row[2],
                              "witnesses": witnesses})
            return beats
    return []


def _stored_reactions(conn: Any, chapter: int) -> Optional[list[dict[str, Any]]]:
    """Reactions in the chapter's current actuals; ``None`` when unknown (no v22 actuals)."""
    rows = _query(
        conn,
        "SELECT payload_json FROM chapter_actuals WHERE chapter=? AND status='current'"
        " ORDER BY created_at DESC LIMIT 1",
        (chapter,),
    )
    if not rows:
        return None
    try:
        payload = json.loads(rows[0][0] or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("reactions"), list):
        return None
    return [r for r in payload["reactions"] if isinstance(r, dict)]


def _reacted(reactors: Iterable[str], witnesses: Sequence[str]) -> bool:
    names = {r for r in reactors if r}
    if witnesses:
        return bool(names & set(witnesses))
    return bool(names)


def _committed_text(conn: Any, chapter: int) -> Optional[str]:
    """Committed prose of an earlier chapter (latest L0 revision); None when unreadable.

    In-memory databases have no project ``l0/`` directory, so they never read files.
    """
    from ..db.l0 import database_file, resolve_draft_path

    if conn is None or database_file(conn) is None:
        return None
    rows = _query(
        conn,
        "SELECT file_path FROM draft_index WHERE chapter=?"
        " ORDER BY revision_round DESC, created_at DESC LIMIT 1",
        (chapter,),
    )
    if not rows or not rows[0][0]:
        return None
    try:
        return resolve_draft_path(conn, str(rows[0][0])).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class _Obligation:
    chapter: int
    summary: str
    witnesses: tuple[str, ...]
    aliases: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def alias_map(self) -> dict[str, list[str]]:
        return {name: list(extra) for name, extra in self.aliases}


def _pending_payoffs(
    conn: Any,
    chapter: int,
    window: int,
    protagonist: Callable[[], Protagonist],
    language: Callable[[], Optional[str]] = lambda: None,
) -> list[_Obligation]:
    """Earlier payoff chapters (within the window) whose reactions are still missing.

    Chapter p owes an aftermath when its contract has a payoff beat, its stored
    current actuals exist (v22) and neither they nor a re-detection on p's
    committed prose show any non-protagonist reaction.  A reaction by p's
    witnesses in any chapter q with p < q < ``chapter`` settles it; evidence for q
    is its stored actuals **plus the detector re-run on q's committed L0 text with
    p's witnesses** — a plain (non-payoff) chapter stores no reactions of its own,
    so its actuals alone could never settle the obligation.
    ``protagonist``/``language`` are only called when such a chapter p exists.
    """
    if conn is None or window <= 0 or chapter <= 1:
        return []
    first = max(1, chapter - window)
    stored = {q: _stored_reactions(conn, q) for q in range(first, chapter)}
    texts: dict[int, Optional[str]] = {}

    def seen(q: int, witnesses: Sequence[str], aliases: Mapping[str, Sequence[str]],
             hero: Protagonist, accept: Callable[[list[str]], bool]) -> bool:
        """Stored actuals first; the committed text is read only when they don't suffice."""
        if accept(_visible_reactors(conn, stored.get(q) or [], hero)):
            return True
        if q not in texts:
            texts[q] = _committed_text(conn, q)
        text = texts[q]
        if not text or not text.strip():
            return False
        return accept([r.reactor for r in detect_reactions(
            text, witnesses, language=language() or None,
            protagonist_names=hero.names, aliases=aliases,
        )])

    out: list[_Obligation] = []
    for p in range(first, chapter):
        if stored[p] is None:          # 无 v22 actuals：未知，不追究
            continue
        beats = payoff_beats(_stored_contract_beats(conn, p))
        if not beats:
            continue
        hero = protagonist()
        witnesses, aliases = _drop_protagonist(conn, _union_witnesses(beats), hero)
        if seen(p, witnesses, aliases, hero, bool):
            continue
        if any(seen(q, witnesses, aliases, hero, lambda rs: _reacted(rs, witnesses))
               for q in range(p + 1, chapter)):
            continue
        out.append(_Obligation(
            p, beats[0].summary, tuple(witnesses),
            tuple((name, tuple(extra)) for name, extra in aliases.items()),
        ))
    return out


# ── 语言 ──────────────────────────────────────────────────────────────────────

def _resolve_language(
    workspace: Mapping[str, Any], extra: Any, conn: Any, draft_text: str,
) -> str:
    """Writing profile (workspace/extra) → project meta → CJK heuristic on the draft."""
    for profile in (workspace.get("writing_profile"),
                    extra.get("writing_profile") if isinstance(extra, Mapping) else None):
        if isinstance(profile, Mapping):
            language = profile.get("content_language") or profile.get("language")
            if language:
                return str(language)
    rows = _query(conn, "SELECT value FROM meta_kv WHERE key='content_language'")
    if rows and rows[0][0]:
        return str(rows[0][0])
    return infer_language(draft_text)


# ── 文案 ──────────────────────────────────────────────────────────────────────

def _names(names: Sequence[str], en: bool, *, sep: Optional[str] = None) -> str:
    items = list(names)
    if sep is not None:
        return sep.join(items)
    if not en:
        return "、".join(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _beat_ref(beats: Sequence[PayoffBeat], en: bool) -> str:
    parts = []
    for beat in beats[:2]:
        summary = beat.summary[:40]
        if en:
            parts.append(f"beat {beat.seq} “{summary}”" if summary else f"beat {beat.seq}")
        else:
            parts.append(f"第{beat.seq}拍「{summary}」" if summary else f"第{beat.seq}拍")
    return (", " if en else "、").join(parts)


def _payoff_anchor(draft_text: str, beats: Sequence[PayoffBeat]) -> str:
    for beat in beats:
        span = find_evidence_span(draft_text, beat.summary) if beat.summary else None
        if span is not None:
            return span.text[:200]
    return ""


def _issue(detail: str, fix: str, category: str, span: str = "") -> dict[str, Any]:
    return {
        "check": "payoff_reaction", "severity": "warn", "detail": detail, "span": span,
        "fix": fix, "repair_scope": "local", "category": category,
    }


def _no_reaction_issue(
    beats: Sequence[PayoffBeat], witnesses: Sequence[str], draft_text: str,
    *, en: bool, window: int,
) -> dict[str, Any]:
    ref = _beat_ref(beats, en)
    if en:
        who = (f"witnesses {_names(witnesses, en, sep='/')} show none" if witnesses
               else "no witnesses are named and no onlookers react")
        detail = (f"Payoff without a reaction: {ref} draws no visible reaction from anyone "
                  f"but the protagonist ({who})")
        whom = _names(witnesses, en) if witnesses else "a rival or the onlookers"
        fix = (f"Right after the payoff action, show {whom} reacting visibly (a changed "
               "expression, stunned silence, a retraction, a shift in attitude), then a line or "
               "two on what the win means")
        if window:
            fix += "; or open the next chapter with that aftermath"
    else:
        who = (f"在场者 {_names(witnesses, en, sep='/')} 没有可见反应" if witnesses
               else "未点名在场者，围观众人也没有反应")
        detail = f"爽点无反应：{ref}之后没有任何非主角角色的可见反应（{who}）"
        whom = _names(witnesses, en) if witnesses else "对手或围观众人"
        fix = (f"在爽点动作之后补写{whom}的可见反应（神色剧变、失语、改口、态度转变等），"
               "再用一两句点明这次收获的分量")
        if window:
            fix += "；也可在下一章开头用余波补上"
    return _issue(detail, fix, "craft.payoff_no_reaction", _payoff_anchor(draft_text, beats))


def _crowd_only_issue(
    missing: Sequence[str], crowd: Sequence[ReactionEvidence], *, en: bool,
) -> dict[str, Any]:
    crowd_names = list(dict.fromkeys(r.reactor for r in crowd))
    span = crowd[0].evidence_spans[0].text if crowd and crowd[0].evidence_spans else ""
    if en:
        detail = (f"Only the crowd reacts to the payoff ({_names(crowd_names, en)}): the named "
                  f"witnesses {_names(missing, en, sep='/')} show no visible reaction")
        fix = (f"Give {_names(missing, en)} at least one concrete reaction (expression, gesture, "
               "retraction or change in attitude) so the payoff lands on a specific character")
    else:
        detail = (f"爽点反应只有群像：已有{_names(crowd_names, en)}的反应，"
                  f"但点名的在场者 {_names(missing, en, sep='/')} 没有可见反应")
        fix = (f"给{_names(missing, en)}至少一处具体反应（表情、动作、改口或态度变化），"
               "让“看对方怎么输”落到具体人物身上")
    return _issue(detail, fix, "craft.payoff_crowd_only", span)


def _deferred_issue(obligation: _Obligation, chapter: int, *, en: bool) -> dict[str, Any]:
    p = obligation.chapter
    witnesses = obligation.witnesses
    summary = obligation.summary[:40]
    if en:
        lead = "The previous chapter's payoff" if p == chapter - 1 else f"Chapter {p}'s payoff"
        payoff = f"chapter {p}'s payoff “{summary}”" if summary else f"chapter {p}'s payoff"
        whom = (f"witnesses {_names(witnesses, en, sep='/')}" if witnesses
                else "the people present")
        detail = (f"{lead} got no reaction; show its aftermath here: {whom} of {payoff} "
                  "still have not reacted visibly")
        target = _names(witnesses, en) if witnesses else "the rivals or onlookers"
        fix = (f"Open this chapter with the aftermath: show {target} reacting to chapter {p}'s "
               "payoff (shock, a retraction, a shift in attitude) and what the gain has changed")
    else:
        lead = "上一章" if p == chapter - 1 else f"第{p}章"
        payoff = f"第{p}章「{summary}」" if summary else f"第{p}章爽点"
        whom = f"在场者 {_names(witnesses, en, sep='/')}" if witnesses else "在场众人"
        detail = f"{lead}爽点缺少反应，本章应补上余波反应：{payoff}的{whom}至今没有可见反应"
        target = _names(witnesses, en) if witnesses else "对手或围观众人"
        fix = (f"在本章开头用一段余波写出{target}对第{p}章那次爽点的反应（震惊、改口、态度转变），"
               "并点明这次收获带来的变化")
    return _issue(detail, fix, "craft.payoff_reaction_deferred")


# ── 扩展点：craft_checks ─────────────────────────────────────────────────────

def craft_checks(ctx: Any) -> list[dict]:
    """爽点无反应 / 只有群像 / 上一章欠余波 —— 全部 warn、只读、确定性。"""
    workspace = getattr(ctx, "workspace", None)
    if not isinstance(workspace, dict):
        return []
    extra = getattr(ctx, "extra", None)
    settings = _ctx_settings(workspace, extra)
    draft_text = str(workspace.get("draft_text") or "")
    if not settings.enabled or not draft_text.strip():
        return []
    conn = getattr(ctx, "conn", None)
    try:
        chapter = int(getattr(ctx, "target_chapter", 0) or 0)
    except (TypeError, ValueError):
        chapter = 0
    beats = payoff_beats(_workspace_beats(workspace))
    get_protagonist = _once(lambda: resolve_protagonist(conn, chapter))
    get_language = _once(lambda: _resolve_language(workspace, extra, conn, draft_text))
    pending = _pending_payoffs(conn, chapter, settings.window, get_protagonist, get_language)
    if not beats and not pending:
        return []

    language = get_language()
    en = is_english(language)
    protagonist = get_protagonist()
    issues: list[dict] = []
    if beats:
        witnesses, aliases = _drop_protagonist(conn, _union_witnesses(beats), protagonist)
        beats = _restrict(beats, witnesses)
        reactions = detect_for_payoff_beats(
            draft_text, beats, language=language,
            protagonist_names=protagonist.names, aliases=aliases,
        )
        if not reactions:
            # 未点名在场者、也不是打脸/当众类的独处爽点（闭关突破、独自获宝）：本章可能
            # 本就无人在场，按"爽点章或下一章"的口径交给余波规则（_pending_payoffs）在
            # 窗口内追究，不在本章立刻告警；窗口为 0 时没有后续规则，照常告警。
            solo = not witnesses and not any(b.audience for b in beats)
            if not (solo and settings.window > 0):
                issues.append(_no_reaction_issue(beats, witnesses, draft_text, en=en,
                                                 window=settings.window))
        elif witnesses and not {r.reactor for r in reactions} & set(witnesses):
            crowd = [r for r in reactions if is_crowd_reactor(r.reactor)]
            issues.append(_crowd_only_issue(witnesses, crowd, en=en))

    for item in pending:
        found = detect_reactions(
            draft_text, item.witnesses, language=language,
            protagonist_names=protagonist.names, aliases=item.alias_map(),
        )
        if not _reacted((r.reactor for r in found), item.witnesses):
            issues.append(_deferred_issue(item, chapter, en=en))
    return issues


# ── 扩展点：enrich_actuals ───────────────────────────────────────────────────

def _context_language(context: Mapping[str, Any]) -> Optional[str]:
    contract = context.get("current_contract")
    for source in (context, contract if isinstance(contract, Mapping) else {}):
        for key in ("content_language", "language"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def enrich_actuals(actuals: Any, draft_text: str, plan_context: Any) -> None:
    """契约 payoff beat 的在场者反应 → ``actuals.reactions``（按 reactor+区间去重）。

    无 DB：不知道主角名与别名。规划误把主角列为 witness 时，主角自己的反应也会
    被记下——所有读取方（``_pending_payoffs``、``settle``）统一用 ``_visible_reactors``
    / ``_is_protagonist`` 剔除主角后再判定。
    """
    if actuals is None or not (draft_text or "").strip():
        return
    context = _context_dict(plan_context)
    beats = payoff_beats(_contract_beats(context))
    if not beats:
        return
    language = _context_language(context) or infer_language(draft_text)
    existing = list(getattr(actuals, "reactions", None) or [])
    seen = {
        (r.reactor, r.evidence_spans[0].start_char, r.evidence_spans[0].end_char)
        for r in existing if r.evidence_spans
    }
    for reaction in detect_for_payoff_beats(draft_text, beats, language=language):
        span = reaction.evidence_spans[0]
        key = (reaction.reactor, span.start_char, span.end_char)
        if key not in seen:
            seen.add(key)
            existing.append(reaction)
    actuals.reactions = existing


# ── 扩展点：draft_brief ──────────────────────────────────────────────────────

_SECRECY_RANK = {"top_secret": 3, "secret": 2, "open_secret": 0, "public": -1}


def _info_gap_targets(conn: Any, workspace: Mapping[str, Any], protagonist_id: str) -> list[str]:
    """Entity ids of this chapter's cast: contract ``must_include_entities``, else recall
    (also when none of the contract's names resolves to an entity or alias)."""
    contract = _context_dict(workspace.get("plan_context")).get("current_contract") or {}
    names = contract.get("must_include_entities") if isinstance(contract, Mapping) else None
    ids: list[str] = []
    if isinstance(names, (list, tuple)):
        for name in names:
            entity_id = _resolve_entity_id(conn, str(name))
            if entity_id and entity_id not in ids:
                ids.append(entity_id)
    if not ids:   # 无契约名单，或名单里的写法一个都对不上实体/别名 → 用召回的活跃角色
        recall = workspace.get("recall_pack")
        for row in (getattr(recall, "entities", None) or [])[:20]:
            if not isinstance(row, Mapping):
                continue
            if row.get("entity_type") not in (None, "character"):
                continue
            if row.get("status") not in (None, "active"):
                continue
            entity_id = str(row.get("id") or "")
            if entity_id and entity_id not in ids:
                ids.append(entity_id)
    return [i for i in ids if i != protagonist_id]


def _forbidden_texts(workspace: Mapping[str, Any]) -> list[str]:
    context = _context_dict(workspace.get("plan_context"))
    contract = context.get("current_contract") or {}
    texts: list[str] = []
    if isinstance(contract, Mapping):
        for key in ("must_not_reveal", "must_not_resolve"):
            value = contract.get(key)
            if isinstance(value, (list, tuple)):
                texts.extend(str(v) for v in value)
    for milestone in context.get("forbidden_reveals") or []:
        if isinstance(milestone, Mapping):
            keywords = (milestone.get("target_state") or {}).get("keywords") or []
            if isinstance(keywords, (list, tuple)):
                texts.extend(str(k) for k in keywords)
    return [t.strip() for t in texts if len(t.strip()) >= 2]


def info_gap_opportunities(
    conn: Any, chapter: int, workspace: Mapping[str, Any], *, protagonist: Protagonist, limit: int,
) -> list[dict[str, Any]]:
    """Secrets the protagonist knows while someone in this chapter does not (as of N-1).

    Returns ``[{secret, state: unaware|misinformed, targets: [names]}]`` ordered by
    secrecy (top_secret first), misinformed before unaware, then the oldest
    protagonist knowledge.  Secrets already public, or that the contract forbids
    revealing, are skipped.  The latest edge per (knower, secret) wins.
    """
    if conn is None or limit <= 0 or not protagonist.entity_id:
        return []
    targets = _info_gap_targets(conn, workspace, protagonist.entity_id)
    if not targets:
        return []
    as_of = chapter - 1
    knowers = [protagonist.entity_id, *targets]
    marks = ",".join("?" for _ in knowers)
    latest: dict[tuple[str, str], tuple[str, int, Optional[str]]] = {}
    for row in _query(
        conn,
        "SELECT knower_entity_id, secret_key, knowledge_state, learned_chapter, secrecy_level"
        f" FROM knowledge_edges WHERE learned_chapter<=? AND knower_entity_id IN ({marks})"
        " ORDER BY knower_entity_id, secret_key, learned_chapter, created_at, id",
        (as_of, *knowers),
    ):
        latest[(str(row[0]), str(row[1]))] = (str(row[2]), int(row[3]), row[4])
    public = {
        str(row[0]) for row in _query(
            conn,
            "SELECT DISTINCT secret_key FROM knowledge_edges WHERE"
            " (public_from_chapter IS NOT NULL AND public_from_chapter<=?)"
            " OR (secrecy_level='public' AND learned_chapter<=?)",
            (as_of, as_of),
        )
    }
    forbidden = _forbidden_texts(workspace)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for (knower, secret), (state, learned, secrecy) in latest.items():
        if knower != protagonist.entity_id or state != "knows" or secret in public:
            continue
        if any(f in secret or secret in f for f in forbidden):
            continue
        for order, target in enumerate(targets):
            target_state = latest.get((target, secret))
            if not target_state or target_state[0] not in ("unaware", "misinformed"):
                continue
            group = groups.setdefault((secret, target_state[0]), {
                "secret": secret, "state": target_state[0], "target_ids": [],
                "rank": (-_SECRECY_RANK.get(secrecy or "", 1),
                         0 if target_state[0] == "misinformed" else 1, learned, order, secret),
            })
            group["target_ids"].append(target)
    out: list[dict[str, Any]] = []
    for group in sorted(groups.values(), key=lambda g: g["rank"])[:limit]:
        names = []
        for entity_id in group["target_ids"]:
            found = _entity_names(conn, entity_id)
            names.append(found[0] if found else entity_id)
        out.append({"secret": group["secret"], "state": group["state"], "targets": names})
    return out


def _info_gap_line(gap: Mapping[str, Any], en: bool) -> str:
    targets = list(gap["targets"])
    secret = gap["secret"]
    who = _names(targets, en)
    if en:
        if gap["state"] == "misinformed":
            verb = "are" if len(targets) > 1 else "is"
            return (f"- Information-gap opportunity: {who} {verb} still mistaken about “{secret}” "
                    "(the protagonist knows the truth) — consider a public reveal or comeuppance")
        verb = "do" if len(targets) > 1 else "does"
        return (f"- Information-gap opportunity: {who} still {verb} not know “{secret}” "
                "(the protagonist does) — consider a public reveal or comeuppance")
    if gap["state"] == "misinformed":
        return f"- 信息差机会：{who} 对「{secret}」仍有误判（主角已知真相）——可安排当众揭晓/打脸"
    return f"- 信息差机会：{who} 还不知道「{secret}」（主角已知）——可安排当众揭晓/打脸"


def _payoff_brief_lines(beats: Sequence[PayoffBeat], en: bool) -> list[str]:
    lines: list[str] = []
    for beat in beats[:3]:
        summary = beat.summary[:60]
        if en:
            ref = f"Beat {beat.seq} “{summary}”" if summary else f"Beat {beat.seq}"
            who = (f"the witnesses {_names(beat.witnesses, en)}" if beat.witnesses
                   else "the people present (no witnesses are named — put a rival or onlookers "
                        "in the scene)")
            lines.append(
                f"- {ref} is a payoff: show the protagonist's action, then visible reactions "
                f"from {who} (a changed expression, stunned silence, a retraction, a shift in "
                "attitude), then make clear what the gain means."
            )
        else:
            ref = f"第{beat.seq}拍「{summary}」" if summary else f"第{beat.seq}拍"
            who = (f"在场者 {_names(beat.witnesses, en)}" if beat.witnesses
                   else "在场众人（本拍未点名在场者，可安排对手或旁观者在场）")
            lines.append(
                f"- {ref}是爽点：先写主角的动作，再写{who}的可见反应"
                "（神色剧变、失语、改口、态度转变），最后点明这次收获有多重要。"
            )
    return lines


def _pending_lines(pending: Sequence[_Obligation], en: bool) -> list[str]:
    lines: list[str] = []
    for item in pending:
        if en:
            whom = f" (witnesses: {_names(item.witnesses, en)})" if item.witnesses else ""
            lines.append(
                f"- Chapter {item.chapter}'s payoff{whom} never showed a visible reaction: open "
                "this chapter with the aftermath and show how they react."
            )
        else:
            whom = f"（在场者 {_names(item.witnesses, en)}）" if item.witnesses else ""
            lines.append(
                f"- 第{item.chapter}章的爽点{whom}还没有写出可见反应：本章开头先补一段余波，"
                "写出他们的反应与态度变化。"
            )
    return lines


def draft_brief(conn: Any, chapter: int, workspace: Any, *, language: str, cfg: Any) -> str:
    """三段式提醒 + 欠下的余波 + 信息差机会；无可提示内容时返回 ""。"""
    settings = _settings(cfg)
    if isinstance(workspace, dict):
        # craft_checks 拿不到 cfg：把本特性的开关/窗口放到同章黑板上供其读取。
        workspace[_CFG_KEY] = {
            "enabled": settings.enabled,
            "reaction_window_chapters": settings.window,
            "info_gap_suggestions": settings.info_gaps,
        }
    else:
        workspace = {}
    if not settings.enabled:
        return ""
    en = is_english(language)
    protagonist = resolve_protagonist(conn, chapter)
    beats = payoff_beats(_workspace_beats(workspace))
    if beats:
        kept, _ = _drop_protagonist(conn, _union_witnesses(beats), protagonist)
        beats = _restrict(beats, kept)
    pending = _pending_payoffs(conn, chapter, settings.window, lambda: protagonist,
                               lambda: language)
    gaps = info_gap_opportunities(
        conn, chapter, workspace, protagonist=protagonist, limit=settings.info_gaps,
    )

    blocks: list[str] = []
    payoff_lines = _payoff_brief_lines(beats, en) + _pending_lines(pending, en)
    if payoff_lines:
        if en:
            header = "## Payoff in three steps (action → witness reaction → why the gain matters)"
            general = "- The thrill is watching the other side lose, not only the win."
        else:
            header = "## 爽点三段式（动作→在场者反应→收获重要性展示）"
            general = "- “看对方怎么输”才是快感来源：别只写主角赢。"
        blocks.append("\n".join([header, general, *payoff_lines]))
    if gaps:
        header = ("## Information gaps (for a public reveal or comeuppance)" if en
                  else "## 信息差（可用于当众揭晓/打脸）")
        blocks.append("\n".join([header, *(_info_gap_line(g, en) for g in gaps)]))
    return "\n\n".join(blocks)


# ── 扩展点：settle ───────────────────────────────────────────────────────────

def _actuals_reactions(actuals: Any) -> list[dict[str, Any]]:
    raw = (actuals.get("reactions") if isinstance(actuals, Mapping)
           else getattr(actuals, "reactions", None))
    out: list[dict[str, Any]] = []
    for item in raw or []:
        if isinstance(item, Mapping):
            out.append(dict(item))
        elif hasattr(item, "model_dump"):
            out.append(item.model_dump(mode="json"))
    return out


def settle(conn: Any, chapter: int, workspace: Any, *, gateway: Any = None, cfg: Any = None) -> dict:
    """只读汇总本章爽点反应覆盖率（写入 pipeline_run.detail_json["extensions"]）。

    口径与 actuals 一致（契约 payoff beat）：读取已持久化的 ``chapter_actuals``
    反应（剔除主角）；契约里没有爽点、而本章 beat sheet（planner 生成）有爽点时，
    直接对正文跑同一个确定性检测器（``source="draft"``）。不写库、不调 LLM。
    """
    settings = _settings(cfg)
    if not settings.enabled:
        return {"skipped": "disabled"}
    workspace = workspace if isinstance(workspace, Mapping) else {}
    draft_text = str(workspace.get("draft_text") or "")
    beats = payoff_beats(_contract_beats(workspace.get("plan_context")))
    reactions = _actuals_reactions(workspace.get("chapter_actuals"))
    source = "actuals"
    if not beats:
        beats = payoff_beats(workspace.get("beats"))
        source = "draft" if beats else source
    witnesses: list[str] = []
    get_protagonist = _once(lambda: resolve_protagonist(conn, chapter))
    if beats:
        protagonist = get_protagonist()
        witnesses, aliases = _drop_protagonist(conn, _union_witnesses(beats), protagonist)
        if source == "draft":
            language = _resolve_language(workspace, None, conn, draft_text)
            reactions = [r.model_dump(mode="json") for r in detect_for_payoff_beats(
                draft_text, _restrict(beats, witnesses), language=language,
                protagonist_names=protagonist.names, aliases=aliases,
            )]
    if reactions and source == "actuals":
        # actuals 由无 DB 的 enrich_actuals 写入：规划把主角列为在场者时会记下主角
        # 自己的反应，这里与 craft_checks 同口径剔除。
        protagonist = get_protagonist()
        reactions = [r for r in reactions
                     if not _is_protagonist(conn, _clean_name(r.get("reactor")), protagonist)]
    reactors = list(dict.fromkeys(str(r.get("reactor") or "") for r in reactions
                                  if r.get("reactor")))
    return {
        "payoff_beats": len(beats),
        "reactions": len(reactions),
        "witnesses": witnesses,
        "reactors": reactors,
        "witnesses_missing": [w for w in witnesses if w not in reactors],
        "source": source,
    }
