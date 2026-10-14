"""F3 极性序列与抑扬上限（v22）：每章必须"转"，同向不超过三。

依据（docs/NovelForge/23 P0-3）：千幻冰云"最惨不能惨过三章，最爽也不能爽过三章"；
Story Grid 每场必须转、避免同向极性链；猫腻"小高潮、平复期和余韵。高潮越小，平复期越短"。

扩展点（见 craft/extensions.py），全部确定性、零 LLM 调用：

- ``enrich_actuals``：回填本章实际转向/转折类型。证据顺序：
  1. 契约有 ``polarity`` 且计划 beats 完成过半 → 采用契约的 polarity/turning_point（计划已执行）；
  2. 否则看已完成 beats 的 ``value_axis``（"A→B" 或 "X↑/X↓"），用中英小词典比较终态与起态，
     每个 beat 得 + / - / ±，再按"最后一个有价值变化的 beat（本章高潮）权重 ×2"求净值：
     净值 >0 → ``+``，<0 → ``-``，持平或高潮 beat 本身得失参半 → ``±``；hook beat 不计
     （章末悬念属于下一章的起因）。"<轴名>：A→B" / "status: low -> high" 先拆出轴名（轴名
     不参与起终态比较——"实力：弱→强"里的"实力"不能算成起态的正面词）；两态情感持平时
     再比较量级词（高/低、强/弱、more/less），好坏由轴名决定（"危险：低→高" 为 -），
     对手的价值物取反（"敌人实力：弱→强" 为 -）；
  3. 仍无法判断 → 保持 None（宁缺毋滥：未知转向会打断连续计数，不会误报）。
- ``settle``：把实际转向写入 ``pacing_state``（按章 upsert，幂等），再按逐章历史**重算**
  ``pacing_cursor.low_streak / polarity_streak / last_polarity``（重写/换稿安全）。
  actuals 未能给出转向时（无契约 beats，或契约未锁定、planner 另写了 beats），用本章实际
  使用的 beats 在终稿中的证据重做第 2 步。``polarity.enabled=False`` 时只把三个计数清零
  （防止旧计数继续驱动提示）。
- ``craft_checks``（均为 warn，中英双语，无历史时静默）：
  a) 本章计划转向会让同向链超过 ``max_same_polarity_streak`` → "本章需反向转折"；
  b) 连续低谷已达 ``max_low_streak`` 且本章计划仍为 - 或未定 → "需要转机"（与 a 不重复报）；
  c) 上一章是大高潮（档位 5，且最后一个 payoff_beat 之后章内未平复），本章既无 aftermath
     beat 也不是低张力开场 → 提示安排余韵。单个 payoff_beat（档位 4）是小爽点，不要求余韵。
  "计划转向"= 契约 polarity；契约未定时用本章 beats 的 value_axis 推断。
- ``draft_brief``：仅在相关时输出 ``## 节奏要求`` / ``## Pacing requirements``——连续低谷/顺境
  后的转向要求（与契约计划转向调和；游标提示已写明"需要转机"时不重复）、大高潮后的余韵
  （与检查 c 同一口径；上一章高张力 beat 越多建议越长）。同时把本项目的 ``cfg.polarity``
  存进 ``workspace["polarity_settings"]``，供同一章的 ``craft_checks`` 读取（检查拿不到 cfg）。

``pacing_state.tension_level`` 刻度（由本章 beats 得出）：1 平缓（仅 setup/aftermath/hook）、
2 有 turn、3 有 tension_point、4 有 payoff_beat（大爽点）、5 payoff_beat 叠加其他高张力 beat
（大高潮）。``pace`` 依次为 slow/steady/fast/climax/climax。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Optional

from ..ids import new_id
from ..planning.actuals import find_evidence_span
from ..planning.contract_fields import normalize_polarity, normalize_turning_point
from .pacing import PacingController, leading_run, trailing_polarities

logger = logging.getLogger(__name__)

SETTINGS_KEY = "polarity_settings"   # draft_brief → craft_checks 的配置通道
_HISTORY_LIMIT = 64
_BIG_CLIMAX_LEVEL = 5                 # tension_level 5：payoff_beat 叠加其他高张力 beat
_LONG_COOLDOWN_WEIGHT = 3             # 上一章 ≥3 个高张力 beat：余韵约占前三分之一
_HIGH_TENSION_TYPES = ("tension_point", "payoff_beat")
_LOW_TENSION_OPENINGS = ("setup", "aftermath")
# 未迁移库的典型报错（SELECT 缺列为 no such column；INSERT 缺列为 has no column named）
_NO_TABLE_MARKERS = ("no such table", "no such column", "has no column named")


# ── 配置 ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PolaritySettings:
    enabled: bool = True
    max_same_polarity_streak: int = 3
    max_low_streak: int = 3


def _settings(source: Any) -> PolaritySettings:
    """Accept a NovelForgeConfig, a PolarityConfig, a dict or None."""
    if isinstance(source, PolaritySettings):
        return source
    if source is None:
        return PolaritySettings()
    if isinstance(source, Mapping):
        pcfg: Any = source.get("polarity", source)
    else:
        pcfg = getattr(source, "polarity", source)

    def get(name: str, default: Any) -> Any:
        if isinstance(pcfg, Mapping):
            return pcfg.get(name, default)
        return getattr(pcfg, name, default)

    def positive_int(name: str, default: int) -> int:
        try:
            return max(1, int(get(name, default)))
        except (TypeError, ValueError):
            return default

    return PolaritySettings(
        enabled=bool(get("enabled", True)),
        max_same_polarity_streak=positive_int("max_same_polarity_streak", 3),
        max_low_streak=positive_int("max_low_streak", 3),
    )


# ── 价值轴情感词典（小而确定；只在 beats 给出方向时使用）────────────────────────

_ZH_POSITIVE = (
    "失而复得", "转危为安", "化险为夷", "绝处逢生", "反败为胜", "扬眉吐气", "真相大白",
    "胜利", "获胜", "大胜", "完胜", "击败", "打败", "击退", "反杀", "碾压", "突破", "认可",
    "承认", "敬畏", "尊重", "尊敬", "崇拜", "希望", "成功", "获得", "收获", "得到", "晋升",
    "晋级", "升级", "进阶", "安全", "安稳", "平安", "自由", "团聚", "重逢", "和解", "信任",
    "荣耀", "荣誉", "翻盘", "逆转", "反击", "转机", "获救", "得救", "脱险", "解脱", "掌控",
    "主动", "优势", "上风", "强大", "崛起", "打脸", "顿悟", "觉醒", "守护", "团结", "喜悦",
    "欢喜", "安心", "圆满", "生机", "光明", "平静", "安宁", "稳定", "认同", "接纳", "结盟",
    "机缘", "奇遇", "复仇", "报仇", "雪耻", "洗冤", "得手", "立威", "震慑", "扬名", "成名",
    "疗伤", "痊愈", "康复", "无敌", "不败", "无伤", "胜", "赢", "爽",
    "修复", "恢复", "重燃", "夺回", "找回", "释放",
)
# 拥有越多越好的"价值物"（也算正面词）：权力↓ = 受挫。它们常作轴名（"实力：弱→强"），
# 见 _split_shift / _subject_sign。
_ZH_GOODS = (
    "权力", "实力", "地位", "修为", "境界", "声望", "名望", "名誉", "财富", "力量", "尊严",
    "底气",
)
_ZH_NEGATIVE = (
    "破裂", "压抑", "受损",
    "乐极生悲", "功亏一篑", "前功尽弃", "危机", "危险", "危局", "危急", "告急", "受辱",
    "屈辱", "羞辱", "侮辱", "失去", "失败", "败北", "惨败", "落败", "受挫", "挫折", "挫败",
    "绝境", "绝路", "绝望", "死亡", "身亡", "濒死", "受伤", "重伤", "背叛", "出卖", "困境",
    "被困", "陷阱", "恐惧", "害怕", "孤立", "孤独", "怀疑", "猜忌", "崩溃", "失控", "劣势",
    "下风", "被动", "囚禁", "被擒", "被俘", "追杀", "损失", "破灭", "落空", "失望", "痛苦",
    "悲痛", "悲伤", "误会", "误解", "分离", "决裂", "威胁", "压迫", "打压", "暴露", "黑暗",
    "波澜", "动荡", "混乱", "失势", "蒙冤", "冤屈", "嘲讽", "轻视", "鄙夷", "不安", "无望",
    "无力", "窘迫", "狼狈", "压力", "败", "输", "死", "伤",
)
_ZH_MIXED = ("得失参半", "喜忧参半", "悲喜交加", "有得有失", "惨胜", "代价", "苦涩", "两难")
# 紧邻正面词之前的否定（"失去了希望"）与紧邻负面词之前的化解（"化解危机"）会翻转该词；
# 只看紧邻（允许"了/的"等虚词），避免"无敌的希望"这类远处字符误判。
_ZH_NEGATORS = (
    "不", "没", "没有", "未", "无", "毫无", "缺乏", "失去", "丧失", "丢失", "错失", "痛失",
    "难以",
)
_ZH_RESOLVERS = (
    "化解", "解除", "摆脱", "脱离", "走出", "逃出", "逃离", "度过", "渡过", "战胜", "克服",
    "平息", "打破", "冲破", "粉碎", "识破", "揭穿", "免于", "远离", "消除", "解开", "洗清",
    "不再", "不", "未", "无", "没",
)
_ZH_FILLERS = "了的得地之也都又 "
_ZH_LEXICON: tuple[tuple[str, int], ...] = tuple(sorted(
    [(w, 1) for w in _ZH_POSITIVE + _ZH_GOODS] + [(w, -1) for w in _ZH_NEGATIVE]
    + [(w, 0) for w in _ZH_MIXED],
    key=lambda item: -len(item[0]),
))

_EN_POSITIVE = frozenset((
    "win", "wins", "won", "winning", "victory", "victorious", "triumph", "triumphant",
    "success", "successful", "breakthrough", "recognition", "recognized", "respect",
    "respected", "awe", "hope", "hopeful", "safe", "safety", "secure", "free", "freedom",
    "freed", "trust", "trusted", "rescue", "rescued", "saved", "reunion", "reunited",
    "reconciled", "reconciliation", "ally", "allies", "alliance", "advantage", "control",
    "confident", "confidence", "acceptance", "accepted", "promoted", "promotion", "upgrade",
    "gain", "gained", "love", "loved", "joy", "relief", "relieved", "peace", "calm",
    "escape", "escaped", "revenge", "vindicated", "vindication", "strength", "stronger",
    "empowered", "glory", "honor", "honour", "praise", "praised", "approval", "belonging",
    "healed", "recovery", "recovered", "solved", "comeback", "upper", "regain", "regained",
    "restored", "repaired", "mended", "rebuilt", "renewed",
))
# goods: more is better ("status down" = a setback); often the axis name ("status: low -> high")
_EN_GOODS = frozenset((
    "power", "status", "wealth", "reputation", "standing", "dignity", "rank", "influence",
    "morale",
))
_EN_POSITIVE = _EN_POSITIVE | _EN_GOODS
_EN_NEGATIVE = frozenset((
    "shattered", "damaged",
    "crisis", "danger", "dangerous", "threat", "threatened", "humiliation", "humiliated",
    "shame", "shamed", "loss", "lost", "lose", "loses", "defeat", "defeated", "failure",
    "failed", "fail", "despair", "desperate", "hopeless", "trapped", "trap", "cornered",
    "captured", "betrayal", "betrayed", "death", "dead", "dying", "wounded", "injured",
    "hurt", "fear", "afraid", "terror", "isolation", "isolated", "alone", "doubt",
    "suspicion", "suspected", "exposed", "chaos", "collapse", "collapsed", "grief", "pain",
    "ruin", "ruined", "disgrace", "disgraced", "setback", "weak", "weaker", "powerless",
    "helpless", "broken", "turmoil", "unrest", "mistrust", "distrust", "disaster",
    "catastrophe", "doom", "doomed", "rejected", "rejection", "abandoned", "guilt",
    "pressure",
))
_EN_MIXED = frozenset(("bittersweet", "pyrrhic", "costly", "cost", "ambivalent", "mixed"))
_EN_NEGATORS = frozenset((
    "no", "not", "never", "without", "lost", "lose", "loses", "losing", "lack", "lacking",
    "less", "gone",
))
_EN_RESOLVERS = frozenset((
    "escape", "escapes", "escaped", "escaping", "overcome", "overcomes", "overcame",
    "survive", "survives", "survived", "resolve", "resolved", "avert", "averted", "beat",
    "beats", "end", "ended", "ends", "from", "defuse", "defused", "no", "not", "never",
    "without",
))
_EN_TOKEN_RE = re.compile(r"[a-z]+")

_ARROW_RE = re.compile(r"\s*(?:→|⟶|⇒|➔|➜|➝|->|=>|—>|－>)\s*")
# "<轴名>：A→B" / "<axis>: A -> B"：轴名不含箭头与冒号，且冒号后必须仍是一个变化
_AXIS_LABEL_RE = re.compile(r"^\s*([^：:→⟶⇒➔➜➝>]{1,20}?)\s*[：:]\s*(\S.*)$")
_ZH_FROM_TO_RE = re.compile(r"^\s*从(.+?)(?:到|至)(.+)$")
_EN_FROM_TO_RE = re.compile(r"^\s*from\s+(.+?)\s+to\s+(.+)$", re.IGNORECASE)
_UP_MARKERS = ("↑", "⬆", "↗", "上升", "提升", "增强", "升高", "高涨")
_DOWN_MARKERS = ("↓", "⬇", "↘", "下降", "降低", "减弱", "跌落", "下滑")
_EN_UP_WORDS = frozenset(("up", "rises", "rising", "increases", "grows", "growing"))
_EN_DOWN_WORDS = frozenset((
    "down", "falls", "falling", "drops", "decreases", "declines", "shrinks", "fades",
))

# 量级词：只描述"多/少、高/低"，本身无好坏——好坏由轴名决定（"危险：低→高" 是 -，
# "实力：弱→强" 是 +）。只在两态情感词持平时作后备比较。
_ZH_MORE = ("高", "强", "多", "大", "满", "增", "涨", "升", "盛", "旺", "重", "有")
_ZH_LESS = ("低", "弱", "少", "小", "空", "减", "降", "跌", "衰", "微", "轻", "无", "零")
_ZH_DEGREE = ("越来越", "非常", "十分", "极其", "极", "更", "较", "很", "最", "太", "稍")
_EN_MORE = frozenset((
    "high", "higher", "highest", "strong", "stronger", "strongest", "more", "most", "many",
    "much", "big", "bigger", "great", "greater", "huge", "full", "up", "rising", "peak",
    "max", "maximum",
))
_EN_LESS = frozenset((
    "low", "lower", "lowest", "weak", "weaker", "weakest", "less", "least", "few", "fewer",
    "little", "small", "smaller", "tiny", "empty", "none", "zero", "down", "gone",
    "minimal", "minimum",
))
_EN_DEGREE = frozenset((
    "very", "too", "quite", "rather", "extremely", "so", "even", "slightly", "a", "the",
    "is", "are", "at", "its", "it",
))
# 轴名指向对手时，对手的"价值物"越多对主角越不利（"敌人实力：弱→强" 是 -）
_ZH_OPPONENTS = (
    "敌人", "敌方", "敌军", "对手", "反派", "仇敌", "仇家", "仇人", "宿敌", "强敌", "对方",
)
_EN_OPPONENTS = frozenset((
    "enemy", "enemies", "rival", "rivals", "opponent", "opponents", "villain", "villains",
    "foe", "foes", "antagonist", "antagonists",
))

_REVELATION_ZH = (
    "发现", "得知", "真相", "揭露", "揭开", "揭示", "识破", "看穿", "秘密", "身份",
    "原来", "竟是", "醒悟", "顿悟", "意识到", "线索", "曝光", "坦白", "告白",
)
_ACTION_ZH = (
    "击败", "打败", "出手", "决定", "选择", "抉择", "出击", "反击", "动手", "拔剑", "挑战",
    "拒绝", "交易", "突破", "营救", "救", "逃", "夺", "闯", "战", "杀", "攻",
)
_REVELATION_EN = frozenset((
    "discovers", "discover", "discovered", "learns", "learn", "learned", "reveals", "reveal",
    "revealed", "revelation", "truth", "secret", "realizes", "realize", "realized",
    "identity", "clue", "exposed", "confession", "confesses", "admits", "uncovers",
))
_ACTION_EN = frozenset((
    "defeats", "defeat", "fights", "fight", "chooses", "choose", "decides", "decide",
    "attacks", "attack", "escapes", "escape", "saves", "save", "kills", "kill", "steals",
    "refuses", "challenges", "strikes", "rescues", "confronts", "duel", "duels",
))


def state_value(text: str) -> Optional[str]:
    """Sentiment of one value state: ``+`` / ``-`` / ``±`` (both or mixed) / None."""
    text = str(text or "").strip().lower()
    if not text:
        return None
    pos = neg = 0
    mixed = False
    taken = [False] * len(text)
    for word, kind in _ZH_LEXICON:          # 最长优先、互不重叠
        start = 0
        while (i := text.find(word, start)) >= 0:
            start = i + 1
            j = i + len(word)
            if any(taken[i:j]):
                continue
            for k in range(i, j):
                taken[k] = True
            if kind == 0:
                mixed = True
                continue
            prefix = text[max(0, i - 6):i].rstrip(_ZH_FILLERS)
            flips = _ZH_NEGATORS if kind > 0 else _ZH_RESOLVERS
            value = -kind if any(prefix.endswith(f) for f in flips) else kind
            pos, neg = (pos + 1, neg) if value > 0 else (pos, neg + 1)
    tokens = _EN_TOKEN_RE.findall(text)
    for idx, token in enumerate(tokens):
        if token in _EN_MIXED:
            mixed = True
            continue
        kind = 1 if token in _EN_POSITIVE else (-1 if token in _EN_NEGATIVE else 0)
        if not kind:
            continue
        window = tokens[max(0, idx - 2):idx]
        flips = _EN_NEGATORS if kind > 0 else _EN_RESOLVERS
        value = -kind if any(w in flips for w in window) else kind
        pos, neg = (pos + 1, neg) if value > 0 else (pos, neg + 1)
    if mixed or (pos and neg):
        return "±"
    if pos:
        return "+"
    if neg:
        return "-"
    return None


def _split_states(axis: str) -> Optional[tuple[str, str]]:
    parts = [p for p in _ARROW_RE.split(axis) if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[-1]
    for pattern in (_ZH_FROM_TO_RE, _EN_FROM_TO_RE):
        match = pattern.match(axis)
        if match:
            return match.group(1), match.group(2)
    return None


def _split_shift(axis: str) -> Optional[tuple[Optional[str], str, str]]:
    """(轴名 or None, 起态, 终态)。

    "实力：弱→强" / "status: low -> high" 的轴名必须单独拿出来：价值物名词（实力/地位/
    status…）本身记为正面，若留在起态里会让起态得 +、终态无词，算成"变差"（符号反了）。
    """
    match = _AXIS_LABEL_RE.match(axis)
    if match:
        states = _split_states(match.group(2))
        if states is not None:
            return match.group(1).strip(), states[0], states[1]
    states = _split_states(axis)
    if states is None:
        return None
    return None, states[0], states[1]


def _magnitude(text: str) -> tuple[int, int]:
    """(净量级, 命中数)：多/高/强 记 +1，少/低/弱 记 -1。"""
    text = str(text or "").lower()
    more = sum(text.count(w) for w in _ZH_MORE)
    less = sum(text.count(w) for w in _ZH_LESS)
    for token in _EN_TOKEN_RE.findall(text):
        more += token in _EN_MORE
        less += token in _EN_LESS
    return more - less, more + less


def _magnitude_only(text: str) -> bool:
    """状态只由量级词（及程度副词、虚词、标点）构成，如 "强"、"更弱"、"low"、"very high"。"""
    text = str(text or "").lower()
    if not _magnitude(text)[1]:
        return False
    if any(t not in _EN_MORE and t not in _EN_LESS and t not in _EN_DEGREE
           for t in _EN_TOKEN_RE.findall(text)):
        return False
    rest = _EN_TOKEN_RE.sub("", text)
    for word in _ZH_DEGREE + _ZH_MORE + _ZH_LESS:
        rest = rest.replace(word, "")
    return not any(ch.isalnum() and ch not in _ZH_FILLERS for ch in rest)


def _names_opponent(text: str) -> bool:
    text = str(text or "").lower()
    return any(w in text for w in _ZH_OPPONENTS) or bool(
        set(_EN_TOKEN_RE.findall(text)) & _EN_OPPONENTS
    )


def _has_goods(text: str) -> bool:
    text = str(text or "").lower()
    return any(w in text for w in _ZH_GOODS) or bool(
        set(_EN_TOKEN_RE.findall(text)) & _EN_GOODS
    )


def _subject_sign(subject: str) -> int:
    """轴名的"越多越好"方向：+1（希望/实力）、-1（危险/压力）、0（看不出）。

    对手的价值物（"敌人实力"、"rival's status"）越多对主角越不利，取反。
    """
    value = state_value(subject)
    if value not in _NUMERIC:
        return 0
    sign = _NUMERIC[value]
    if _names_opponent(subject) and _has_goods(subject):
        sign = -sign
    return sign


def _trend(axis: str) -> Optional[tuple[str, int]]:
    """``X↑`` / ``X↓`` / "trust down" → (subject, ±1)."""
    up = [m for m in _UP_MARKERS if m in axis]
    down = [m for m in _DOWN_MARKERS if m in axis]
    subject = axis
    for marker in up + down:
        subject = subject.replace(marker, " ")
    direction = (1 if up else 0) - (1 if down else 0)
    if not up and not down:
        tokens = _EN_TOKEN_RE.findall(axis.lower())
        if tokens and tokens[-1] in _EN_UP_WORDS:
            direction, subject = 1, " ".join(tokens[:-1])
        elif tokens and tokens[-1] in _EN_DOWN_WORDS:
            direction, subject = -1, " ".join(tokens[:-1])
    return (subject, direction) if direction else None


_NUMERIC = {"+": 1, "-": -1}


def axis_polarity(axis: Any) -> Optional[str]:
    """Direction of one beat's value shift (``"A→B"``, ``"从A到B"``, ``"X↑"``).

    A bare axis name ("power", "生死") carries no direction and returns None.
    """
    axis = str(axis or "").strip()
    if not axis:
        return None
    shift = _split_shift(axis)
    if shift is not None:
        return _shift_polarity(*shift)
    trend = _trend(axis)
    if trend is not None:
        sign = _subject_sign(trend[0]) * trend[1]
        if sign:
            return "+" if sign > 0 else "-"
    return None


def _shift_polarity(label: Optional[str], start: str, end: str) -> Optional[str]:
    """One "A→B" shift; ``label`` is the axis name split off "<label>：A→B"."""
    s, e = state_value(start), state_value(end)
    if e == "±":
        return "±"
    s_num, e_num = _NUMERIC.get(s or "", 0), _NUMERIC.get(e or "", 0)
    # "实力弱→强" / "status low -> high"（无冒号）：一侧是"价值词+量级词"、另一侧只有
    # 量级词时，价值词是轴名而不是状态——不能拿它和量级词比情感。（"强→失败"里的
    # "失败"不带量级词，仍是状态。）
    if (label is None and e is None and s in _NUMERIC and _magnitude(start)[1]
            and _magnitude_only(end)):
        label, s_num = start, 0
    elif (label is None and s is None and e in _NUMERIC and _magnitude(end)[1]
            and _magnitude_only(start)):
        label, e_num = end, 0
    delta = e_num - s_num
    if delta:
        return "+" if delta > 0 else "-"
    # 两态情感持平（或都没有情感词）：比较量级，好坏由轴名或两态共享的价值词决定。
    if label is not None:
        sign = _subject_sign(label)
    elif s == e and s in _NUMERIC:
        sign = _subject_sign(start)
    else:
        return None
    step = _magnitude(end)[0] - _magnitude(start)[0]
    if not sign or not step:
        return None
    return "+" if sign * step > 0 else "-"


def aggregate_turns(signs: Iterable[Optional[str]]) -> Optional[str]:
    """Chapter turn from per-beat shifts; the last (climax) shift weighs double."""
    values = [s for s in signs if s in ("+", "-", "±")]
    if not values:
        return None
    if values[-1] == "±":
        return "±"
    last = len(values) - 1
    net = sum(_NUMERIC.get(s, 0) * (2 if i == last else 1) for i, s in enumerate(values))
    return "+" if net > 0 else ("-" if net < 0 else "±")


def _beat_type(beat: Any) -> str:
    return str(beat.get("beat_type") or "") if isinstance(beat, Mapping) else ""


def _turn_beats(beats: Iterable[Any]) -> list[Mapping[str, Any]]:
    """Beats that can carry the chapter's turn (hooks open the *next* chapter)."""
    return [b for b in beats if isinstance(b, Mapping) and _beat_type(b) != "hook"]


def infer_turning_point(beats: Iterable[Any]) -> Optional[str]:
    """``revelation`` / ``action`` from the latest turn-carrying beat that says which."""
    for beat in reversed(_turn_beats(beats)):
        if _beat_type(beat) not in ("turn", "payoff_beat", "tension_point"):
            continue
        text = f"{beat.get('summary') or ''} {beat.get('value_axis') or ''}".lower()
        tokens = set(_EN_TOKEN_RE.findall(text))
        if any(w in text for w in _REVELATION_ZH) or tokens & _REVELATION_EN:
            return "revelation"
        if any(w in text for w in _ACTION_ZH) or tokens & _ACTION_EN:
            return "action"
    return None


def infer_polarity(beats: Iterable[Any]) -> Optional[str]:
    """Chapter polarity inferred from the beats' value axes (see module docstring)."""
    return aggregate_turns(axis_polarity(b.get("value_axis")) for b in _turn_beats(beats))


# ── enrich_actuals ────────────────────────────────────────────────────────────

def _contract_of(plan_context: Any) -> Mapping[str, Any]:
    if plan_context is None:
        return {}
    if isinstance(plan_context, Mapping):
        contract = plan_context.get("current_contract")
    else:
        contract = getattr(plan_context, "current_contract", None)
    return contract if isinstance(contract, Mapping) else {}


def _contract_beats(contract: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    beats = contract.get("beats")
    if not isinstance(beats, list):
        return []
    return [b for b in beats if isinstance(b, Mapping)]


def enrich_actuals(actuals: Any, draft_text: str, plan_context: Any) -> None:
    """Back-fill ``actuals.polarity`` / ``actuals.turning_point``; never raises."""
    try:
        if getattr(actuals, "polarity", None) is not None:
            return   # 已由更可靠的来源填写
        contract = _contract_of(plan_context)
        beats = _contract_beats(contract)
        evidence = list(getattr(actuals, "completed_beats", None) or [])
        if not beats or not evidence:
            return
        done_seqs = {int(e.beat_seq) for e in evidence if getattr(e, "completed", False)}
        done = [
            b for i, b in enumerate(beats, 1)
            if _seq(b, i) in done_seqs
        ]
        executed = len(done_seqs) * 2 >= len(evidence)
        planned = normalize_polarity(contract.get("polarity"))
        if planned and executed:
            actuals.polarity = planned
            actuals.turning_point = (
                normalize_turning_point(contract.get("turning_point"))
                or infer_turning_point(done)
            )
            return
        inferred = infer_polarity(done)
        if inferred:
            actuals.polarity = inferred
            actuals.turning_point = infer_turning_point(done)
    except Exception:   # 扩展失败不能影响 actuals 主流程
        logger.exception("polarity.enrich_actuals failed")


def _seq(beat: Mapping[str, Any], fallback: int) -> int:
    try:
        return int(beat.get("seq") or fallback)
    except (TypeError, ValueError):
        return fallback


# ── beats → 张力档位 ──────────────────────────────────────────────────────────

_PACE_BY_LEVEL = {1: "slow", 2: "steady", 3: "fast", 4: "climax", 5: "climax"}


def tension_level(beat_types: Iterable[str]) -> int:
    types = list(beat_types)
    payoffs = types.count("payoff_beat")
    highs = sum(types.count(t) for t in _HIGH_TENSION_TYPES)
    if payoffs and highs >= 2:
        return 5
    if payoffs:
        return 4
    if highs:
        return 3
    if "turn" in types:
        return 2
    return 1


# ── settle ────────────────────────────────────────────────────────────────────

def _actual_turn(
    actuals: Any, beats: list[Mapping[str, Any]], draft_text: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(polarity, turning_point, source) for the committed chapter."""
    if actuals is not None:
        get = actuals.get if isinstance(actuals, Mapping) else (
            lambda name, default=None: getattr(actuals, name, default)
        )
        polarity = normalize_polarity(get("polarity"))
        if polarity:
            return polarity, normalize_turning_point(get("turning_point")), "actuals"
    # 契约未锁定时 planner 会另写 beats，actuals 评估的却是契约 beats：用本章实际使用的
    # beats 在终稿中的证据再推断一次（锁定契约时 beats 相同，结论与 enrich 一致）。
    if not beats or not draft_text:
        return None, None, None
    done = [
        b for b in beats
        if find_evidence_span(draft_text, str(b.get("summary") or "")) is not None
    ]
    polarity = infer_polarity(done)
    if polarity is None:
        return None, None, None
    return polarity, infer_turning_point(done), "beats"


def settle(conn: sqlite3.Connection, chapter: int, workspace: dict, *,
           gateway: Any = None, cfg: Any = None) -> dict:
    """Upsert this chapter's ``pacing_state`` and recompute the cursor streaks."""
    settings = _settings(cfg)
    pacing = PacingController()
    savepoint = "f3_polarity_settle"
    if not settings.enabled:
        try:
            conn.execute(
                "UPDATE pacing_cursor SET low_streak=0, polarity_streak=0, last_polarity=NULL"
                " WHERE id=1 AND (low_streak<>0 OR polarity_streak<>0"
                "                 OR last_polarity IS NOT NULL)"
            )
        except sqlite3.OperationalError as exc:
            if not _missing_schema(exc):
                raise
        return {"enabled": False}

    beats = [b for b in (workspace.get("beats") or []) if isinstance(b, Mapping)]
    draft_text = str(workspace.get("draft_text") or "")
    polarity, turning_point, source = _actual_turn(
        workspace.get("chapter_actuals"), beats, draft_text,
    )
    types = [_beat_type(b) for b in beats]
    level = tension_level(types)
    pace = _PACE_BY_LEVEL[level] if beats else None
    notes = json.dumps({
        "source": source,
        "beat_types": types,
        "aftermath": "aftermath" in types,
        "climax": level,
    }, ensure_ascii=False, separators=(",", ":"))
    report: dict[str, Any] = {
        "chapter": chapter, "polarity": polarity, "turning_point": turning_point,
        "source": source, "tension_level": level, "pace": pace,
    }
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        conn.execute(
            "INSERT INTO pacing_state(id, chapter, tension_level, pace, notes,"
            "  polarity, turning_point) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(chapter) DO UPDATE SET"
            "   tension_level=excluded.tension_level, pace=excluded.pace,"
            "   notes=excluded.notes, polarity=excluded.polarity,"
            "   turning_point=excluded.turning_point",
            (new_id("pace"), chapter, level, pace, notes, polarity, turning_point),
        )
        report.update(pacing.recompute_polarity_streaks(conn))
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except sqlite3.Error as exc:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if isinstance(exc, sqlite3.OperationalError) and _missing_schema(exc):
            # 未迁移到 v22 的旧库：报告跳过，不把本章标成降级
            return {**report, "skipped": "schema", "error": str(exc)}
        raise
    return report


def _missing_schema(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _NO_TABLE_MARKERS)


# ── 历史读取（检查与提示共用）─────────────────────────────────────────────────

@dataclass(frozen=True)
class _History:
    chapter: int                       # 正在规划/检查的章
    trail: list[Optional[str]]         # 第 chapter-1 章起向前、章号连续的实际转向
    prev_level: Optional[int]          # 上一章的张力档位（无记录为 None）
    prev_types: Optional[tuple[str, ...]] = None   # 上一章 beats 的类型序列（未知为 None）

    def streak(self) -> tuple[Optional[str], int]:
        """(末尾连续转向, 章数)；末章转向未知时为 (None, 0)。"""
        head = self.trail[0] if self.trail else None
        return head, leading_run(self.trail, head)

    def span(self, length: int) -> tuple[int, int]:
        return self.chapter - length, self.chapter - 1

    def owed_cooldown(self) -> int:
        """上一章留下、需要本章余韵来平复的高潮档位；0 = 不需要。

        只有大高潮（档位 5：payoff_beat 叠加其他高张力 beat）才要求余韵——单个
        payoff_beat 是小爽点，"高潮越小，平复期越短"，不必每章都开场就平复。上一章在
        最后一个 payoff_beat 之后已经安排了 setup/aftermath（章内已平复）时也不需要。
        """
        level = self.prev_level or 0
        if level < _BIG_CLIMAX_LEVEL:
            return 0
        types = self.prev_types
        if types and "payoff_beat" in types:
            last_payoff = max(i for i, t in enumerate(types) if t == "payoff_beat")
            if any(t in _LOW_TENSION_OPENINGS for t in types[last_payoff + 1:]):
                return 0
        return level

    def climax_weight(self) -> int:
        """上一章高张力 beat 的个数（类型未知时按档位 5 的最低值 2 计）。"""
        if self.prev_types:
            return sum(1 for t in self.prev_types if t in _HIGH_TENSION_TYPES)
        return 2


def _load_history(conn: sqlite3.Connection, chapter: int) -> _History:
    rows = conn.execute(
        "SELECT chapter, polarity, tension_level, notes FROM pacing_state"
        " WHERE chapter<? ORDER BY chapter DESC LIMIT ?",
        (chapter, _HISTORY_LIMIT),
    ).fetchall()
    pairs = [(int(r["chapter"]), r["polarity"]) for r in rows]
    contiguous = bool(pairs) and pairs[0][0] == chapter - 1
    trail = trailing_polarities(pairs) if contiguous else []
    if contiguous:
        prev_level = int(rows[0]["tension_level"])
        prev_types = _note_types(rows[0]["notes"])
    else:
        prev_level, prev_types = _planned_level(conn, chapter - 1)
    return _History(chapter=chapter, trail=trail, prev_level=prev_level,
                    prev_types=prev_types)


def _note_types(notes: Any) -> Optional[tuple[str, ...]]:
    """``pacing_state.notes`` JSON written by :func:`settle` → its ordered beat types."""
    try:
        data = json.loads(notes) if notes else None
    except (TypeError, ValueError):
        return None
    types = data.get("beat_types") if isinstance(data, Mapping) else None
    if not isinstance(types, list):
        return None
    return tuple(str(t or "") for t in types)


def _planned_level(
    conn: sqlite3.Connection, chapter: int,
) -> tuple[Optional[int], Optional[tuple[str, ...]]]:
    """No settled row for the previous chapter: fall back to its planned beats."""
    if chapter < 1:
        return None, None
    for sql in (
        "SELECT beat_type FROM beats WHERE chapter=? AND status<>'dropped' ORDER BY seq",
        "SELECT b.beat_type FROM chapter_cards c"
        " JOIN chapter_plan_version_beats b ON b.plan_version_id=c.current_plan_version_id"
        " WHERE c.chapter=? ORDER BY b.seq",
    ):
        try:
            types = [str(r[0]) for r in conn.execute(sql, (chapter,)).fetchall()]
        except sqlite3.Error:
            continue
        if types:
            return tension_level(types), tuple(types)
    return None, None


def _planned_contract(workspace: Mapping[str, Any], conn: Any, chapter: int) -> Mapping[str, Any]:
    contract = _contract_of(workspace.get("plan_context"))
    if contract or conn is None:
        return contract
    try:
        row = conn.execute(
            "SELECT v.payload_json FROM chapter_cards c JOIN chapter_plan_versions v"
            " ON v.id=c.current_plan_version_id WHERE c.chapter=?",
            (chapter,),
        ).fetchone()
        payload = json.loads(row[0]) if row and row[0] else {}
    except (sqlite3.Error, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, Mapping) else {}


# ── craft_checks ─────────────────────────────────────────────────────────────

def _is_english(language: Any) -> bool:
    return str(language or "").lower().replace("_", "-").startswith("en")


def _ctx_language(ctx: Any) -> str:
    workspace = getattr(ctx, "workspace", None) or {}
    extra = getattr(ctx, "extra", None) or {}
    profile = workspace.get("writing_profile") or extra.get("writing_profile")
    if not isinstance(profile, Mapping) and getattr(ctx, "conn", None) is not None:
        try:
            from ..writing_profiles import get_project_writing_profile
            profile = get_project_writing_profile(ctx.conn, chapter=ctx.target_chapter)
        except Exception:
            profile = {}
    if not isinstance(profile, Mapping):
        return ""
    return str(profile.get("content_language") or profile.get("language") or "")


def _streak_issue(
    history: _History, planned: Optional[str], settings: PolaritySettings, english: bool,
) -> list[dict]:
    head, length = history.streak()
    if head not in ("+", "-") or length == 0:
        return []
    first, last = history.span(length)
    if head == "-" and planned in ("-", None) and length >= settings.max_low_streak:
        limit = settings.max_low_streak
        if english:
            plan = "still plans a negative turn" if planned == "-" else "plans no clear upturn"
            detail = (
                f"The last {length} chapters ({first}–{last}) all turned against the "
                f"protagonist (polarity -), and this chapter {plan}. Going past {limit} "
                "chapters of unrelieved pressure makes readers drop the book."
            )
            fix = (
                "This chapter needs a turn: move the situation toward the protagonist's goal "
                "(polarity + or ±): an ally arrives, a scheme is seen through, a small win, "
                "a relationship warms."
            )
        else:
            plan = "计划转向仍为 -" if planned == "-" else "没有安排明确的转机"
            detail = (
                f"已连续 {length} 章低谷（第 {first}–{last} 章转向均为 -），本章{plan}；"
                f"最惨不能连续超过 {limit} 章，压抑过久读者容易弃书。"
            )
            fix = (
                "本章需要转机：安排一次让局面向主角目标好转的转折（转向 + 或 ±），"
                "如获得助力、识破阴谋、小胜一场或关系回暖。"
            )
        return [{
            "check": "polarity_turn", "severity": "warn", "detail": detail, "fix": fix,
            "repair_scope": "structural", "category": "craft.polarity_turn",
        }]
    if planned != head or length < settings.max_same_polarity_streak:
        return []
    limit = settings.max_same_polarity_streak
    if english:
        kind = "positive" if head == "+" else "negative"
        detail = (
            f"This chapter plans another {kind} turn ({head}); with chapters {first}–{last} "
            f"that makes {length + 1} in a row (limit {limit}). A run that long in one "
            "direction goes numb."
        )
        fix = "Turn the other way this chapter: " + (
            "a setback, a cost or a new threat (polarity - or ±) so the wins keep their weight."
            if head == "+" else "an upturn toward the goal (polarity + or ±)."
        )
    else:
        mood = "顺风" if head == "+" else "压抑"
        detail = (
            f"本章计划转向 {head}，将与第 {first}–{last} 章连成 {length + 1} 章同向"
            f"（上限 {limit} 章）；一路{mood}，读者会麻木。"
        )
        fix = "本章需反向转折：" + (
            "安排受挫、代价或新危机（转向 - 或 ±），让胜利有分量。"
            if head == "+" else "安排转机（转向 + 或 ±）。"
        )
    return [{
        "check": "polarity_streak", "severity": "warn", "detail": detail, "fix": fix,
        "repair_scope": "structural", "category": "craft.polarity_streak",
    }]


def _aftermath_issue(history: _History, beats: list[Mapping[str, Any]], english: bool) -> list[dict]:
    if not beats or not history.owed_cooldown():
        return []
    types = [_beat_type(b) for b in beats]
    if "aftermath" in types or types[0] in _LOW_TENSION_OPENINGS:
        return []
    prev = history.chapter - 1
    if english:
        detail = (
            f"Chapter {prev} was a major climax, but this chapter has no aftermath beat and "
            f"opens straight on a high-tension {types[0] or 'unknown'} beat; without a "
            "cooldown the climax loses its weight."
        )
        fix = (
            "Open with an aftermath: witnesses react, the win sinks in, the characters "
            "breathe (the bigger the climax, the longer the cooldown) before the next "
            "conflict starts."
        )
    else:
        detail = (
            f"上一章（第 {prev} 章）是大高潮（爽点叠加高张力），本章既没有余韵（aftermath）"
            f"beat，开场又直接是高张力的 {types[0] or '未知'} beat；缺少平复期，高潮的分量会被冲淡。"
        )
        fix = "在本章开头安排余韵：在场者的反应、收获的消化与人物喘息（高潮越大平复越长），再进入新冲突。"
    return [{
        "check": "aftermath", "severity": "warn", "detail": detail, "fix": fix,
        "repair_scope": "local", "category": "craft.aftermath",
    }]


def craft_checks(ctx: Any) -> list[dict]:
    """Pre-commit polarity/aftermath warnings (read-only, deterministic)."""
    workspace = getattr(ctx, "workspace", None) or {}
    settings = _settings(workspace.get(SETTINGS_KEY))
    conn = getattr(ctx, "conn", None)
    chapter = int(getattr(ctx, "target_chapter", 0) or 0)
    if not settings.enabled or conn is None or chapter < 2:
        return []
    try:
        history = _load_history(conn, chapter)
    except sqlite3.Error:
        return []   # 旧库无 pacing_state / 极性列
    if not history.trail and history.prev_level is None:
        return []
    beats = [b for b in (workspace.get("beats") or []) if isinstance(b, Mapping)]
    planned = (
        normalize_polarity(_planned_contract(workspace, conn, chapter).get("polarity"))
        or infer_polarity(beats)
    )
    english = _is_english(_ctx_language(ctx))
    return (
        _streak_issue(history, planned, settings, english)
        + _aftermath_issue(history, beats, english)
    )


# ── draft_brief ──────────────────────────────────────────────────────────────

def draft_brief(conn: sqlite3.Connection, chapter: int, workspace: dict, *,
                language: str, cfg: Any) -> str:
    """``## 节奏要求`` block for the planner and the drafter (empty when irrelevant)."""
    settings = _settings(cfg)
    if isinstance(workspace, dict):
        workspace[SETTINGS_KEY] = asdict(settings)
    if not settings.enabled or conn is None or chapter < 2:
        return ""
    try:
        history = _load_history(conn, chapter)
    except sqlite3.Error:
        return ""
    english = _is_english(language)
    contract = _contract_of(workspace.get("plan_context"))
    planned = normalize_polarity(contract.get("polarity"))
    lines: list[str] = []

    head, length = history.streak()
    # 游标提示（chapter_goal 里的"节奏提示：已连续 N 章低谷…"）已说了同一件事时不重复；
    # 契约仍计划 - 的调和建议是提示里没有的信息，照常输出。
    pacing_state = workspace.get("pacing_state")
    hint_covers_turn = bool(
        getattr(pacing_state, "needs_turn", False)
        and getattr(pacing_state, "low_streak", None) == length
        and getattr(pacing_state, "max_low_streak", None) == settings.max_low_streak
    )
    if head == "-" and length >= min(settings.max_low_streak, settings.max_same_polarity_streak):
        first, last = history.span(length)
        if planned is None and not hint_covers_turn:
            lines.append(
                f"- The last {length} chapters ({first}–{last}) all turned against the "
                "protagonist (polarity -): this chapter must turn toward the goal "
                "(polarity + or ±)." if english else
                f"- 已连续 {length} 章低谷（第 {first}–{last} 章转向均为 -）：本章必须安排转机，"
                "让局面向主角目标好转（转向 + 或 ±）。"
            )
        elif planned == "-":
            lines.append(
                f"- The last {length} chapters ({first}–{last}) all turned against the "
                "protagonist, and the contract still plans a negative turn: give at least a "
                "glimmer of an upturn (an ally, a clue) so the chapter lands as ±." if english
                else
                f"- 已连续 {length} 章低谷（第 {first}–{last} 章转向均为 -），而本章契约仍计划"
                "转向 -：至少给出一线转机（如盟友出现、线索浮现），让本章转向成为 ±。"
            )
    elif head == "+" and length >= settings.max_same_polarity_streak:
        first, last = history.span(length)
        if planned is None:
            lines.append(
                f"- The last {length} chapters ({first}–{last}) all turned in the "
                "protagonist's favor (polarity +): turn the other way this chapter (a "
                "setback, a cost or a new threat; polarity - or ±) so the wins don't go numb."
                if english else
                f"- 已连续 {length} 章顺境（第 {first}–{last} 章转向均为 +）：本章需反向转折——"
                "受挫、代价或新危机（转向 - 或 ±），别让爽感麻木。"
            )
        elif planned == "+":
            lines.append(
                f"- The last {length} chapters ({first}–{last}) all turned in the "
                "protagonist's favor, and the contract plans another win: attach a real cost "
                "or a new threat to it so the chapter lands as ±." if english else
                f"- 已连续 {length} 章顺境（第 {first}–{last} 章转向均为 +），而本章契约仍计划"
                "转向 +：请为胜利附上代价或新隐患，让本章转向成为 ±。"
            )

    planned_types = [_beat_type(b) for b in _contract_beats(contract)]
    if history.owed_cooldown() and "aftermath" not in planned_types:
        big = history.climax_weight() >= _LONG_COOLDOWN_WEIGHT
        prev = chapter - 1
        if english:
            length_hint = (
                "a fuller aftermath, roughly the first third of the chapter" if big
                else "a short aftermath, a beat or two"
            )
            lines.append(
                f"- Chapter {prev} was a major climax: open with an aftermath beat (witnesses "
                "react, the win sinks in, the characters breathe). The bigger the climax, "
                f"the longer the cooldown; here: {length_hint}."
            )
        else:
            length_hint = "较充分的篇幅（约前三分之一）" if big else "一小段（一两个节拍）"
            lines.append(
                f"- 上一章（第 {prev} 章）是大高潮：本章开头安排余韵（aftermath beat）——在场者的"
                f"反应、收获的消化、人物喘息；高潮越大平复越长，本章建议用{length_hint}。"
            )

    if not lines:
        return ""
    heading = "## Pacing requirements" if english else "## 节奏要求"
    return heading + "\n" + "\n".join(lines)
