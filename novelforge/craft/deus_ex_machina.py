"""F2 "天降外挂"拦截器（v22）：能力必须先被读者理解，代价必须付出。

依据（docs/NovelForge/23 P0-2）：乌贼"不能随意加入新东西或新外挂来解决问题"；阅文金手指五原则；
Sanderson 三定律（读者理解程度决定能用它解决多少冲突 / Limitations > Powers / 先扩展再新增）；
Brooks 第二情节点后"no newcomers"。

扩展点（见 craft/extensions.py）：
- ``craft_checks``：解决冲突的实体/金手指/物品必须早于本章登场且展示过；金手指代价须付出；
  卷进度 > 75% 后首次出现的新实体告警；卷内新增能力 / 旧能力新用法比值提示。

五项检查全部确定性（无 LLM）、只读、默认 ``warn``（设计说明见
``docs/NovelForge/v22/F2-deus-ex-machina.md``）：

a) newcomer_resolver       高潮段由本章首次出现的具名实体解决
b) undemonstrated_ability  高潮段动用此前从未展示/使用过的金手指（soft_magic 时措辞降级）
c) unpaid_cost             有 cost_json 的金手指本章被使用，但提案与正文都没有兑现代价
d) late_newcomer           卷进度超过阈值后首次出现的具名实体（跳过无名龙套，≤3 条）
e) new_ability_ratio       本章新增能力，且本卷"新增能力数 ÷ 旧能力新用法数"超阈值

"首次出现"判定（宁漏勿误报）：此前任一章（1..N-1）留有痕迹即视为已铺垫——
``entities.first_appear_chapter < N``、非 retcon 的 facts、未被否决的 fact_candidates、
或早前章节正文（``drafts_fts``，只含各章当前修订）提到过名字/别名。没有任何痕迹时：
``first_appear_chapter >= N``、注册表 ``valid_from_chapter >= N`` 或本章 gimmick_rule
提案（且注册表无此金手指）直接判"新"；其余情况只有在 1..N-1 章已写草稿全部入了
FTS 索引（能证明"从未提及"）时才判"新"，否则视为"未知"而静默。第 0 章（seed 默认章号）
的 facts 是设定库而非读者可见的出场，不算铺垫。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

CHECK_NEWCOMER = "newcomer_resolver"
CHECK_UNDEMONSTRATED = "undemonstrated_ability"
CHECK_UNPAID_COST = "unpaid_cost"
CHECK_LATE_NEWCOMER = "late_newcomer"
CHECK_ABILITY_RATIO = "new_ability_ratio"
_CATEGORY_PREFIX = "craft.deus_ex_machina."

_MAX_ISSUES_PER_CHECK = 3
# 能"出手解决冲突"的实体类型；地点不参与 a/d。
_RESOLVER_TYPES = frozenset({"character", "item", "faction", "concept"})
_PAYOFF_WINDOW_MARGIN = 0.1      # payoff beat 按序号比例估算正文窗口时两侧的余量
_SPAN_RADIUS = 100               # 证据句向两侧最多延伸的字符数
_MAX_HISTORY_TEXT_LOOKUPS = 16   # 单次检查最多扫描早前正文的次数（成本上界）

_LATIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 '’\-.]*$")
_CJK = re.compile(r"[\u3400-\u9fff]")
_ZH_SENTENCE_END = "。！？!?\n"
_EN_SENTENCE_END = ".!?\n"
_CLAUSE_SEPARATORS = "，,；;：:"

# ── 无名龙套（d 一律跳过；a 对提案派生的名字也跳过）──────────────────────────
_ZH_EXTRAS = frozenset({
    "黑衣人", "蒙面人", "神秘人", "陌生人", "老者", "老人", "老妇", "老妪", "少年", "少女",
    "青年", "男子", "女子", "男人", "女人", "大汉", "壮汉", "汉子", "侍卫", "守卫", "护卫",
    "卫兵", "士兵", "兵士", "小厮", "丫鬟", "侍女", "仆人", "店小二", "小二", "掌柜", "伙计",
    "路人", "众人", "弟子", "长老", "村民", "百姓", "客人", "来人", "刺客", "杀手", "书生",
    "道士", "和尚", "乞丐", "老板", "孩子", "妇人", "此人", "那人", "对方", "敌人", "某人",
    "主角", "主人公", "大家", "有人", "他们", "我们", "你们",
})
_ZH_EXTRA_PREFIX = re.compile(r"^((一|两|几|数|三|四|五)(个|名|位|群|伙|队|众|批|帮)|某|这|那|此|该)")
_EN_EXTRAS = frozenset({
    "guard", "guards", "stranger", "man", "woman", "boy", "girl", "child", "soldier",
    "soldiers", "merchant", "innkeeper", "servant", "crowd", "old man", "old woman",
    "someone", "everyone", "somebody", "nobody", "villager", "villagers", "assassin",
    "priest", "monk", "beggar", "elder", "disciple", "captain", "the guard", "the stranger",
    "the man", "the woman", "the crowd", "the protagonist", "he", "she", "they", "we", "you",
})
_EN_ARTICLE = re.compile(r"^(a|an|the|some|this|that)\s", re.IGNORECASE)

# ── 使用动词（c/e 判定"本章用过"；b 只要求出现在高潮段）──────────────────────
_ZH_USE_VERBS = (
    "施展", "催动", "发动", "使用", "动用", "使出", "激发", "激活", "祭出", "运转", "运起",
    "爆发", "开启", "释放", "引动", "召唤", "唤出", "斩出", "轰出", "打出", "劈出", "借助",
    "借用", "凭借", "启动", "调动", "燃烧", "献祭", "加持", "化作",
)
_ZH_NEGATION = ("没有", "并未", "不曾", "未曾", "无法", "不敢", "不能", "没", "未", "不", "别", "莫", "勿")
_EN_USE_VERB = re.compile(
    r"\b(?:us(?:e|ed|es|ing)|invok(?:e|ed|es|ing)|activat(?:e|ed|es|ing)|unleash(?:ed|es|ing)?"
    r"|cast(?:s|ing)?|channel(?:l?ed|s|l?ing)?|summon(?:ed|s|ing)?|trigger(?:ed|s|ing)?"
    r"|wield(?:ed|s|ing)?|ignit(?:e|ed|es|ing)|tapp(?:ed|ing)\s+into"
    r"|(?:call|calls|called|calling|draw|draws|drew|drawing)\s+(?:on|upon))\b",
    re.IGNORECASE,
)
_EN_NEGATION = re.compile(
    r"(?:\bnot|\bnever|n't|\bwithout|\bno longer|\brefused to)\W+(?:\w+\W+)?$", re.IGNORECASE,
)

# ── 代价关键词（cost_json → 正文里可检索的词）────────────────────────────────
_COST_SCHEMA_KEYS = frozenset({
    "type", "kind", "amount", "value", "qty", "quantity", "unit", "units", "desc",
    "description", "note", "notes", "detail", "details", "cost", "costs", "price", "duration",
    "cooldown", "per_use", "level", "severity", "target", "name", "label", "text",
})
_ZH_COST_VERB_PREFIX = re.compile(
    r"^(消耗|燃烧|损失|付出|牺牲|折损|献祭|透支|损耗|扣除|减少|降低|失去|丧失|折|耗|损|扣)"
)
_ZH_COST_RESULT_SUFFIX = re.compile(r"(耗尽|枯竭|透支|受损|大损|衰竭|折损|流失|减半|尽失)$")
_ZH_QUANTITY = re.compile(r"[0-9０-９一二三四五六七八九十百千万两半数几]+[年月日天成点倍次滴口层份个缕道条名]?")
_EN_STOPWORDS = frozenset({
    "cost", "costs", "price", "pays", "paid", "with", "from", "that", "this", "each", "every",
    "uses", "used", "using", "into", "some", "temporary", "temporarily", "after", "before",
    "user", "their", "them", "they", "until", "unit", "units", "amount", "point", "points",
    "once", "times", "minutes", "hours", "days", "weeks", "months", "year", "years",
})


# ── 数据视图 ────────────────────────────────────────────────────────────────

@dataclass
class _Entity:
    name: str
    names: tuple[str, ...]
    entity_type: str = ""            # 空 = 由提案派生、类型未知
    entity_id: Optional[str] = None
    first_appear_chapter: Optional[int] = None


@dataclass
class _Gimmick:
    name: str
    names: tuple[str, ...]
    gimmick_id: Optional[str] = None
    valid_from: Optional[int] = None
    cost: Any = None
    defining_fact_ids: tuple[str, ...] = ()
    proposed_rule: bool = False      # 本章有 gimmick_rule 提案
    use_proposals: list[Mapping] = field(default_factory=list)

    def new_at(self, chapter: int) -> bool:
        """本章（或之后）才设定：注册表 valid_from >= N，或注册表没有、本章提案定义。"""
        if self.gimmick_id is not None:
            return self.valid_from is not None and self.valid_from >= chapter
        return self.proposed_rule


@dataclass
class _Use:
    """一条 gimmick_use 提案：是否付了代价、evidence 在正文中的位置（定位不到为 None）。"""
    paid_cost: bool
    span: Optional[tuple[int, int]]


Region = tuple[int, int]


# ── 入口 ────────────────────────────────────────────────────────────────────

def craft_checks(ctx: Any) -> list[dict]:
    """F2 入口（CraftCheckSkill 扩展接缝）：返回 warn 级 issue dict 列表，只读、可重入。"""
    workspace = getattr(ctx, "workspace", None)
    if not isinstance(workspace, Mapping):
        return []
    cfg = _plausibility_config(workspace)
    if not cfg.enabled:
        return []
    draft = str(workspace.get("draft_text") or "")
    chapter = _as_int(getattr(ctx, "target_chapter", None))
    if not draft.strip() or chapter is None or chapter < 1:
        return []
    conn = getattr(ctx, "conn", None)
    english = _is_english(ctx, workspace, conn, chapter)
    plan_context = _plan_context(workspace, conn, chapter)
    contract = _contract(plan_context)
    beats = _beats(workspace.get("beats")) or _beats(contract.get("beats"))
    proposals = [p for p in (workspace.get("proposals") or []) if isinstance(p, Mapping)]

    guard = _Guard(conn, chapter, draft, english, cfg, proposals)
    regions = (
        _resolving_regions(len(draft), beats, _tail_ratio(cfg))
        if _is_resolving(contract, beats) else []
    )

    issues: list[dict] = []
    flagged: set[str] = set()
    if regions and chapter > 1:
        # 第 1 章没有"此前"，铺垫类检查（a/b）无从谈起。
        issues += guard.check_undemonstrated(regions, flagged)
        issues += guard.check_newcomer_resolver(regions, flagged)
    issues += guard.check_unpaid_cost()
    span = _volume_span(conn, chapter, plan_context) if chapter > 1 else None
    if span is not None:
        # 卷级检查同样从第 2 章起：开篇章节没有"已有能力/已登场人物"可言。
        progress = _volume_progress(span, chapter)
        threshold = float(cfg.late_newcomer_progress)
        if progress is not None and progress > threshold:
            issues += guard.check_late_newcomer(progress, threshold, flagged)
        issues += guard.check_ability_ratio(span)
    return issues


# ── 核心 ────────────────────────────────────────────────────────────────────

class _Guard:
    def __init__(
        self,
        conn: Optional[sqlite3.Connection],
        chapter: int,
        draft: str,
        english: bool,
        cfg: Any,
        proposals: list[Mapping],
    ) -> None:
        self.chapter = chapter
        self.draft = draft
        self.english = english
        self.cfg = cfg
        self.history = _History(conn, chapter, english)
        self._mentions: dict[str, list[int]] = {}
        entity_rows, alias_rows = _load_entity_registry(conn)
        self.gimmicks = _build_gimmicks(conn, proposals, entity_rows, alias_rows)
        gimmick_names = {n for g in self.gimmicks for n in g.names}
        self.entities = _build_entities(entity_rows, alias_rows, proposals, gimmick_names)

    # ― 正文工具 ―

    def mentions(self, name: str) -> list[int]:
        if name not in self._mentions:
            self._mentions[name] = _find_all(self.draft, name)
        return self._mentions[name]

    def first_mention(
        self, names: Iterable[str], regions: Optional[list[Region]] = None,
    ) -> Optional[Region]:
        best: Optional[Region] = None
        for name in names:
            for pos in self.mentions(name):
                if regions is not None and not _in_regions(pos, regions):
                    continue
                if best is None or pos < best[0]:
                    best = (pos, pos + len(name))
                break
        return best

    def span(self, loc: Optional[Region]) -> str:
        if loc is None:
            return ""
        return _sentence(self.draft, loc[0], loc[1], self.english)

    def uses(self, gimmick: _Gimmick) -> list[_Use]:
        out: list[_Use] = []
        for proposal in gimmick.use_proposals:
            new = _proposal_new(proposal)
            out.append(_Use(
                paid_cost=_non_empty(_parse_json(new.get("paid_cost_json"))),
                span=_locate_evidence(self.draft, proposal),
            ))
        return out

    def usage(self, gimmick: _Gimmick) -> tuple[bool, Optional[Region]]:
        """本章是否"用"了该金手指：gimmick_use 提案，或"名字 + 未被否定的使用动词"同句。"""
        uses = self.uses(gimmick)
        if uses:
            located = next((u.span for u in uses if u.span is not None), None)
            return True, located or self.first_mention(gimmick.names)
        for name in gimmick.names:
            for pos in self.mentions(name):
                if _sentence_uses(self.draft, pos, pos + len(name), self.english):
                    return True, (pos, pos + len(name))
        return False, None

    # ― 判定 ―

    def entity_new(self, entity: _Entity) -> Optional[bool]:
        """True=本章首次出现；False=此前已出现；None=无法判断（静默）。"""
        first = entity.first_appear_chapter
        if first is not None and first < self.chapter:
            return False
        if self.history.entity_seen(entity.entity_id, entity.names):
            return False
        seen = self.history.text_mentions(entity.names)
        if seen:
            return False
        if first is not None:              # first_appear_chapter >= N 是正面证据
            return True
        if seen is None or not self.history.text_available():
            return None
        return True

    def gimmick_undemonstrated(self, gimmick: _Gimmick) -> Optional[bool]:
        """True=此前从未展示/使用；False=有展示；None=无法判断（静默）。"""
        if gimmick.gimmick_id and self.history.gimmick_used_before(gimmick.gimmick_id):
            return False
        if self.history.gimmick_fact_before(gimmick.names, gimmick.defining_fact_ids):
            return False
        seen = self.history.text_mentions(gimmick.names)
        if seen:
            return False
        if gimmick.new_at(self.chapter):
            return True
        if seen is None or not self.history.text_available():
            return None
        return True

    # ― a) 高潮由本章新登场者解决 ―

    def check_newcomer_resolver(self, regions: list[Region], flagged: set[str]) -> list[dict]:
        hits: list[tuple[int, str, _Entity, Region]] = []
        for entity in self.entities:
            loc = self.first_mention(entity.names, regions)
            if loc is not None and self.entity_new(entity) is True:
                hits.append((loc[0], entity.name, entity, loc))
        issues = []
        for _, _, entity, loc in sorted(hits, key=lambda h: h[:2])[:_MAX_ISSUES_PER_CHECK]:
            flagged.add(entity.name)
            debut = self.first_mention(entity.names)
            debut_in_climax = debut is not None and _in_regions(debut[0], regions)
            issues.append(_issue(
                CHECK_NEWCOMER, self.span(loc), "structural",
                *_newcomer_text(entity, debut_in_climax, self.english),
            ))
        return issues

    # ― b) 未经展示的能力解决冲突 ―

    def check_undemonstrated(self, regions: list[Region], flagged: set[str]) -> list[dict]:
        soft = bool(self.cfg.soft_magic)
        hits: list[tuple[int, str, _Gimmick, Region]] = []
        for gimmick in self.gimmicks:
            loc = self.first_mention(gimmick.names, regions) or next(
                (u.span for u in self.uses(gimmick)
                 if u.span is not None and _in_regions(u.span[0], regions)),
                None,
            )
            if loc is not None and self.gimmick_undemonstrated(gimmick) is True:
                hits.append((loc[0], gimmick.name, gimmick, loc))
        issues = []
        for _, _, gimmick, loc in sorted(hits, key=lambda h: h[:2])[:_MAX_ISSUES_PER_CHECK]:
            flagged.update(gimmick.names)
            issues.append(_issue(
                CHECK_UNDEMONSTRATED, self.span(loc), "structural",
                *_undemonstrated_text(
                    gimmick.name, gimmick.new_at(self.chapter), soft, self.english,
                ),
            ))
        return issues

    # ― c) 代价未付 ―

    def check_unpaid_cost(self) -> list[dict]:
        hits: list[tuple[int, str, Optional[Region], list[str]]] = []
        for gimmick in self.gimmicks:
            if not _non_empty(gimmick.cost):
                continue
            used, loc = self.usage(gimmick)
            if not used or any(u.paid_cost for u in self.uses(gimmick)):
                continue
            keywords = _cost_keywords(gimmick.cost)
            if not keywords:
                continue        # 预提交数据无从判断（cost_json 无可检索的词）→ 宁漏勿误报
            if any(_contains_keyword(self.draft, kw) for kw in keywords):
                continue
            pos = loc[0] if loc is not None else len(self.draft)
            hits.append((pos, gimmick.name, loc, keywords))
        return [
            _issue(CHECK_UNPAID_COST, self.span(loc), "local",
                   *_unpaid_text(name, keywords[:3], self.english))
            for _, name, loc, keywords in sorted(hits, key=lambda h: h[:2])[:_MAX_ISSUES_PER_CHECK]
        ]

    # ― d) 卷末新人/新设定 ―

    def check_late_newcomer(
        self, progress: float, threshold: float, flagged: set[str],
    ) -> list[dict]:
        hits: list[tuple[int, str, Region]] = []
        for entity in self.entities:
            if entity.name in flagged or _is_extra(entity.name):
                continue
            loc = self.first_mention(entity.names)
            if loc is not None and self.entity_new(entity) is True:
                hits.append((loc[0], entity.name, loc))
        for gimmick in self.gimmicks:
            if gimmick.name in flagged or not gimmick.new_at(self.chapter):
                continue
            loc = self.first_mention(gimmick.names)
            if loc is not None and self.gimmick_undemonstrated(gimmick) is True:
                hits.append((loc[0], gimmick.name, loc))
        issues = []
        for _, name, loc in sorted(hits)[:_MAX_ISSUES_PER_CHECK]:
            flagged.add(name)
            issues.append(_issue(
                CHECK_LATE_NEWCOMER, self.span(loc), "structural",
                *_late_text(name, progress, threshold, self.english),
            ))
        return issues

    # ― e) 新增能力 vs 旧能力新用法 ―

    def check_ability_ratio(self, span: tuple[int, Optional[int]]) -> list[dict]:
        start = span[0]
        chapter_new: list[tuple[int, str, Optional[Region]]] = []
        earlier_new = 0
        old: list[_Gimmick] = []
        for gimmick in self.gimmicks:
            if gimmick.new_at(self.chapter):
                loc = self.first_mention(gimmick.names)
                if gimmick.proposed_rule or loc is not None:
                    chapter_new.append(
                        (loc[0] if loc else len(self.draft), gimmick.name, loc)
                    )
            elif gimmick.valid_from is None:
                continue        # 只有 use 提案、注册表里没有：既不算新也不算旧
            elif gimmick.valid_from >= start:
                earlier_new += 1
            else:
                old.append(gimmick)
        if not chapter_new:
            return []           # 本章没新增能力：提示不可执行，不逐章刷屏
        new_count = earlier_new + len(chapter_new)
        old_uses = self.history.old_gimmick_uses(start)
        old_uses += sum(1 for g in old if self.usage(g)[0])
        if new_count < 3 or new_count / max(1, old_uses) <= float(self.cfg.new_ability_ratio_warn):
            return []
        chapter_new.sort(key=lambda h: h[:2])
        names = [name for _, name, _ in chapter_new][:3]
        return [_issue(
            CHECK_ABILITY_RATIO, self.span(chapter_new[0][2]), "structural",
            *_ratio_text(new_count, old_uses, names, self.english),
        )]


def _build_gimmicks(conn, proposals, entity_rows, alias_rows) -> list[_Gimmick]:
    id_to_name = {row[0]: str(row[1] or "").strip() for row in entity_rows}
    aliases_by_name: dict[str, list[str]] = {}
    for entity_id, alias in alias_rows:
        if entity_id in id_to_name:
            aliases_by_name.setdefault(id_to_name[entity_id], []).append(alias)

    by_name: dict[str, _Gimmick] = {}
    for gid, name, valid_from, cost_json, fact_id, source_fact_id in _load_gimmicks(conn):
        name = str(name or "").strip()
        if not _usable_name(name):
            continue
        by_name[name] = _Gimmick(
            name=name,
            names=tuple(n for n in _unique([name, *aliases_by_name.get(name, [])])
                        if _usable_name(n)),
            gimmick_id=gid,
            valid_from=_as_int(valid_from),
            cost=_parse_json(cost_json),
            defining_fact_ids=tuple(x for x in (fact_id, source_fact_id) if x),
        )
    for proposal in proposals:
        new = _proposal_new(proposal)
        facet = str(new.get("facet") or "")
        name = str(new.get("gimmick_name") or "").strip()
        if facet not in {"gimmick_rule", "gimmick_use"} or not _usable_name(name):
            continue
        gimmick = by_name.setdefault(name, _Gimmick(name=name, names=(name,)))
        if facet == "gimmick_use":
            gimmick.use_proposals.append(proposal)
            continue
        gimmick.proposed_rule = True
        if not _non_empty(gimmick.cost):
            gimmick.cost = _parse_json(new.get("cost_json"))
    return sorted(by_name.values(), key=lambda g: g.name)


def _build_entities(entity_rows, alias_rows, proposals, gimmick_names) -> list[_Entity]:
    aliases: dict[str, list[str]] = {}
    for entity_id, alias in alias_rows:
        aliases.setdefault(entity_id, []).append(alias)
    out: list[_Entity] = []
    known: set[str] = set(gimmick_names)
    for entity_id, name, entity_type, first_appear in entity_rows:
        name = str(name or "").strip()
        names = tuple(n for n in _unique([name, *aliases.get(entity_id, [])]) if _usable_name(n))
        known.update(names)
        if not names or entity_type not in _RESOLVER_TYPES or name in gimmick_names:
            continue
        out.append(_Entity(
            name=name, names=names, entity_type=str(entity_type),
            entity_id=entity_id, first_appear_chapter=_as_int(first_appear),
        ))
    # 本章提案提到、注册表里没有的具名实体：只在早前正文可证"从未出现"时才判新。
    for name in _proposal_entity_names(proposals):
        if name in known or _is_extra(name):
            continue
        known.add(name)
        out.append(_Entity(name=name, names=(name,)))
    return out


# ── 历史证据（只读 SQL；任何 sqlite 错误都退化为"无证据/无法判断"）────────────

class _History:
    def __init__(self, conn: Optional[sqlite3.Connection], chapter: int, english: bool) -> None:
        self.conn = conn
        self.chapter = chapter
        self.english = english
        self._text_available: Optional[bool] = None
        self._text_cache: dict[str, Optional[bool]] = {}
        self._lookups = 0

    def _exists(self, sql: str, params: tuple) -> bool:
        if self.conn is None:
            return False
        try:
            return self.conn.execute(sql, params).fetchone() is not None
        except sqlite3.Error:
            return False

    def text_available(self) -> bool:
        """1..N-1 章凡有草稿的都已进 drafts_fts，才能由"没搜到"推出"从未提及"。"""
        if self._text_available is None:
            self._text_available = False
            if self.conn is not None and self.chapter > 1:
                try:
                    indexed = {
                        int(r[0]) for r in self.conn.execute(
                            "SELECT DISTINCT CAST(chapter_no AS INTEGER) FROM drafts_fts"
                            " WHERE CAST(chapter_no AS INTEGER) BETWEEN 1 AND ?",
                            (self.chapter - 1,),
                        )
                    }
                    drafted = {
                        int(r[0]) for r in self.conn.execute(
                            "SELECT DISTINCT chapter FROM draft_index"
                            " WHERE chapter BETWEEN 1 AND ?",
                            (self.chapter - 1,),
                        )
                    }
                    self._text_available = bool(indexed) and drafted <= indexed
                except (sqlite3.Error, TypeError, ValueError):
                    self._text_available = False
        return self._text_available

    def text_mentions(self, names: Iterable[str]) -> Optional[bool]:
        """早前章节正文是否出现过任一名字：True / False / None（无法判断）。"""
        if self.conn is None or self.chapter <= 1:
            return None
        unknown = False
        for name in names:
            if name not in self._text_cache:
                self._text_cache[name] = self._scan_text(name)
            found = self._text_cache[name]
            if found:
                return True
            unknown = unknown or found is None
        return None if unknown else False

    def _scan_text(self, name: str) -> Optional[bool]:
        if self._lookups >= _MAX_HISTORY_TEXT_LOOKUPS:
            return None
        self._lookups += 1
        if self.english:
            # 英文索引是小写词元串（含词形变体）：同一段落里名字的全部词元都在即算提到。
            # 多词名可能因此偏向"已出现"——只会少报，不会误报。
            from ..tokenizer import tokenize
            tokens = sorted(set(tokenize(name, "en-US").split()))
            if not tokens:
                return None
            cond = " AND ".join("instr(' ' || body || ' ', ?) > 0" for _ in tokens)
            params: tuple = (self.chapter - 1, *(f" {t} " for t in tokens))
        else:
            # 中文索引是分词后空格拼接的原文：去掉空格即可做与分词无关的精确子串匹配。
            cond = "instr(replace(body, ' ', ''), ?) > 0"
            params = (self.chapter - 1, name.replace(" ", ""))
        try:
            row = self.conn.execute(
                "SELECT 1 FROM drafts_fts WHERE CAST(chapter_no AS INTEGER) BETWEEN 1 AND ?"
                f" AND {cond} LIMIT 1",
                params,
            ).fetchone()
        except sqlite3.Error:
            return None
        return row is not None

    def entity_seen(self, entity_id: Optional[str], names: tuple[str, ...]) -> bool:
        if not names or self.chapter <= 1:
            return False
        ph = ",".join("?" * len(names))
        eid = entity_id or ""
        last = self.chapter - 1
        return self._exists(
            "SELECT 1 FROM facts WHERE status != 'retconned'"
            " AND valid_from_chapter BETWEEN 1 AND ?"
            f" AND (entity_id = ? OR subject IN ({ph})) LIMIT 1",
            (last, eid, *names),
        ) or self._exists(
            "SELECT 1 FROM fact_candidates WHERE source_chapter BETWEEN 1 AND ?"
            " AND status IN ('proposed','pending_review','promoted')"
            f" AND (entity_id = ? OR json_extract(proposal_json, '$.entity') IN ({ph})"
            f"      OR json_extract(proposal_json, '$.new.subject') IN ({ph})) LIMIT 1",
            (last, eid, *names, *names),
        )

    def gimmick_used_before(self, gimmick_id: str) -> bool:
        return self._exists(
            "SELECT 1 FROM gimmick_usage_log WHERE gimmick_id = ? AND use_chapter < ? LIMIT 1",
            (gimmick_id, self.chapter),
        )

    def gimmick_fact_before(self, names: tuple[str, ...], exclude: tuple[str, ...]) -> bool:
        """除定义它的 fact 外，1..N-1 章有提到该金手指的 canon fact（展示/使用记录）。"""
        if not names or self.chapter <= 1:
            return False
        ph = ",".join("?" * len(names))
        ex = ",".join("?" * len(exclude)) if exclude else "''"
        return self._exists(
            "SELECT 1 FROM facts WHERE status = 'canon'"
            " AND valid_from_chapter BETWEEN 1 AND ?"
            f" AND id NOT IN ({ex})"
            " AND COALESCE(json_extract(detail_json, '$.facet'), '') != 'gimmick_rule'"
            f" AND (subject IN ({ph}) OR object IN ({ph})"
            f"      OR json_extract(detail_json, '$.gimmick_name') IN ({ph})) LIMIT 1",
            (self.chapter - 1, *exclude, *names, *names, *names),
        )

    def old_gimmick_uses(self, volume_start: int) -> int:
        """本卷 start..N-1 章里、卷前已有金手指的新用法数：distinct (gimmick, chapter)。"""
        if self.conn is None:
            return 0
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM (SELECT DISTINCT u.gimmick_id, u.use_chapter"
                " FROM gimmick_usage_log u JOIN gimmick_rules g ON g.id = u.gimmick_id"
                " WHERE g.valid_from_chapter < ? AND u.use_chapter >= ? AND u.use_chapter < ?)",
                (volume_start, volume_start, self.chapter),
            ).fetchone()
        except sqlite3.Error:
            return 0
        return int(row[0] or 0) if row else 0


def _load_entity_registry(conn) -> tuple[list[tuple], list[tuple]]:
    if conn is None:
        return [], []
    try:
        entities = [
            (r[0], r[1], r[2], r[3]) for r in conn.execute(
                "SELECT id, canonical_name, entity_type, first_appear_chapter FROM entities"
                " ORDER BY canonical_name"
            )
        ]
    except sqlite3.Error:
        return [], []
    try:
        aliases = [
            (r[0], str(r[1] or "").strip()) for r in conn.execute(
                "SELECT entity_id, alias FROM entity_aliases ORDER BY alias"
            )
        ]
    except sqlite3.Error:
        aliases = []
    return entities, aliases


def _load_gimmicks(conn) -> list[tuple]:
    """全部有效金手指。不做 as_of 截断（recall_pack 只到 N-1）：本章新设定的正是要拦的。"""
    if conn is None:
        return []
    try:
        return [
            (r[0], r[1], r[2], r[3], r[4], r[5]) for r in conn.execute(
                "SELECT gr.id, gr.gimmick_name, gr.valid_from_chapter, gr.cost_json,"
                " gr.fact_id, gr.source_fact_id"
                " FROM gimmick_rules gr LEFT JOIN facts f ON f.id = gr.source_fact_id"
                " WHERE gr.source_fact_id IS NULL OR f.status = 'canon'"
                " ORDER BY gr.gimmick_name"
            )
        ]
    except sqlite3.Error:
        return []


# ── 卷进度（与 Orchestrator._volume_progress 同口径；craft 不导入 orchestrator）──

def _volume_span(conn, chapter: int, plan_context: Any) -> Optional[tuple[int, Optional[int]]]:
    """本章所属卷的 (start, end)；end=None 表示该卷仍在写（无进度，只做 e）。"""
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT start_chapter, end_chapter FROM volumes"
                " WHERE start_chapter IS NOT NULL AND end_chapter IS NOT NULL"
                "   AND start_chapter<=? AND end_chapter>=?"
                " ORDER BY volume_no LIMIT 1",
                (chapter, chapter),
            ).fetchone()
            if row is not None:
                return int(row[0]), int(row[1])
            row = conn.execute(
                "SELECT start_chapter FROM volumes"
                " WHERE start_chapter IS NOT NULL AND end_chapter IS NULL AND start_chapter<=?"
                " ORDER BY start_chapter DESC, volume_no DESC LIMIT 1",
                (chapter,),
            ).fetchone()
            return (int(row[0]), None) if row is not None else None
        except (sqlite3.Error, TypeError, ValueError):
            pass
    volume = getattr(plan_context, "current_volume", None)
    if isinstance(volume, Mapping):
        start = _as_int(volume.get("start_chapter"))
        end = _as_int(volume.get("end_chapter"))
        if start is not None and start <= chapter and (end is None or end >= chapter):
            return start, end
    return None


def _volume_progress(span: tuple[int, Optional[int]], chapter: int) -> Optional[float]:
    start, end = span
    if end is None or end <= start:
        return None
    return (chapter - start) / (end - start)


# ── 上下文解析 ──────────────────────────────────────────────────────────────

def _plausibility_config(workspace: Mapping) -> Any:
    from ..config import PlausibilityConfig
    cfg = workspace.get("cfg")
    if isinstance(cfg, PlausibilityConfig):
        return cfg
    nested = getattr(cfg, "plausibility", None)
    return nested if isinstance(nested, PlausibilityConfig) else PlausibilityConfig()


def _tail_ratio(cfg: Any) -> float:
    try:
        value = float(cfg.resolving_tail_ratio)
    except (TypeError, ValueError):
        value = 0.4
    return min(1.0, max(0.05, value))


def _is_english(ctx: Any, workspace: Mapping, conn, chapter: int) -> bool:
    profile = workspace.get("writing_profile")
    if not isinstance(profile, Mapping):
        extra = getattr(ctx, "extra", None)
        profile = extra.get("writing_profile") if isinstance(extra, Mapping) else None
    if not isinstance(profile, Mapping) and conn is not None:
        try:
            from ..writing_profiles import get_project_writing_profile
            profile = get_project_writing_profile(conn, chapter=chapter)
        except Exception:
            profile = None
    language = ""
    if isinstance(profile, Mapping):
        language = str(profile.get("content_language") or profile.get("language") or "")
    return language.lower().replace("_", "-").startswith("en")


def _plan_context(workspace: Mapping, conn, chapter: int) -> Any:
    if "plan_context" in workspace:
        return workspace.get("plan_context")
    if conn is None:
        return None
    # 治理端点只给 draft/beats/proposals：按章号只读地取一次契约与卷信息。
    try:
        from ..planning.context import gather_plan_context
        return gather_plan_context(conn, chapter, lookahead_chapters=0)
    except Exception:
        return None


def _contract(plan_context: Any) -> Mapping:
    contract = getattr(plan_context, "current_contract", None)
    if contract is None and isinstance(plan_context, Mapping):
        contract = plan_context.get("current_contract")
    return contract if isinstance(contract, Mapping) else {}


def _beats(value: Any) -> list[Mapping]:
    if not isinstance(value, (list, tuple)):
        return []
    return [b for b in value if isinstance(b, Mapping)]


def _is_resolving(contract: Mapping, beats: list[Mapping]) -> bool:
    """解决章：契约 must_resolve 非空，或契约 beats / 实际 beats 含 payoff_beat。"""
    if _non_empty(contract.get("must_resolve")):
        return True
    return any(
        b.get("beat_type") == "payoff_beat"
        for b in [*_beats(contract.get("beats")), *beats]
    )


def _resolving_regions(length: int, beats: list[Mapping], tail_ratio: float) -> list[Region]:
    """高潮/兑现段（按 beat 序号比例估算，预提交阶段拿不到逐 beat 证据）。

    有 payoff_beat：取每个 payoff beat 的比例窗口（第 i/n 个 → [i/n−10%, (i+1)/n+10%)）。
    没有（仅 must_resolve）：取正文末尾 tail_ratio；若最后一个 beat 是 hook，再去掉末尾
    min(1/n, tail_ratio/2) 的钩子段——章末新冒出的反派/伏笔是钩子，不是"解决者"。
    """
    n = len(beats)
    regions: list[Region] = []
    for i, beat in enumerate(beats):
        if beat.get("beat_type") == "payoff_beat":
            lo = max(0.0, i / n - _PAYOFF_WINDOW_MARGIN)
            hi = min(1.0, (i + 1) / n + _PAYOFF_WINDOW_MARGIN)
            regions.append((int(length * lo), int(math.ceil(length * hi))))
    if not regions:
        hi = 1.0
        if n >= 2 and beats[-1].get("beat_type") == "hook":
            hi -= min(1.0 / n, tail_ratio / 2)
        regions.append((int(length * (1.0 - tail_ratio)), int(math.ceil(length * hi))))
    regions.sort()
    merged: list[Region] = []
    for lo, hi in regions:
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _in_regions(pos: int, regions: list[Region]) -> bool:
    return any(lo <= pos < hi for lo, hi in regions)


# ── 提案解析 ────────────────────────────────────────────────────────────────

def _proposal_new(proposal: Mapping) -> Mapping:
    new = proposal.get("new")
    return new if isinstance(new, Mapping) else {}


def _proposal_entity_names(proposals: list[Mapping]) -> list[str]:
    names: list[str] = []
    for proposal in proposals:
        new = _proposal_new(proposal)
        raw = [proposal.get("entity"), new.get("subject"), new.get("user_entity"), new.get("owner")]
        if proposal.get("fact_type") == "item" or new.get("facet") == "item":
            raw.append(new.get("object"))
        for value in raw:
            name = value.strip() if isinstance(value, str) else ""
            if name and name not in names and _looks_like_name(name):
                names.append(name)
    return names


def _locate_evidence(draft: str, proposal: Mapping) -> Optional[Region]:
    refs = proposal.get("evidence_refs")
    for ref in refs if isinstance(refs, list) else []:
        quote = str(ref.get("quote") or "").strip() if isinstance(ref, Mapping) else ""
        if len(quote) >= 2:
            pos = draft.find(quote)
            if pos >= 0:
                return pos, pos + len(quote)
    return None


# ── 名字与正文 ──────────────────────────────────────────────────────────────

def _usable_name(name: str) -> bool:
    """单字中文名、无字母的英文串噪声太大，不参与匹配。"""
    name = name.strip()
    return len(name) >= 2 and (bool(_CJK.search(name)) or any(ch.isalpha() for ch in name))


def _looks_like_name(name: str) -> bool:
    """提案里的自由文本是否像专名：中文 2-8 字纯汉字；英文 1-4 词且首词大写。"""
    if not _usable_name(name):
        return False
    if _CJK.search(name):
        return len(name) <= 8 and all(_CJK.match(ch) or ch == "·" for ch in name)
    words = name.split()
    if not 1 <= len(words) <= 4:
        return False
    if not all(re.fullmatch(r"[A-Za-z][A-Za-z'’\-.]*", w) for w in words):
        return False
    return words[0][0].isupper()


def _is_extra(name: str) -> bool:
    """无名龙套（"黑衣人""一名侍卫""the guard"）不算具名新人。"""
    if _CJK.search(name):
        return name in _ZH_EXTRAS or bool(_ZH_EXTRA_PREFIX.match(name))
    if name.casefold() in _EN_EXTRAS or not name[:1].isupper():
        return True
    rest = name.split()[1:]
    return bool(_EN_ARTICLE.match(name)) and not any(w[:1].isupper() for w in rest)


def _find_all(text: str, name: str) -> list[int]:
    if _LATIN_NAME.match(name):
        pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])")
        return [m.start() for m in pattern.finditer(text)]
    out, pos = [], text.find(name)
    while pos >= 0:
        out.append(pos)
        pos = text.find(name, pos + len(name))
    return out


def _bounds(text: str, start: int, end: int, ends: str) -> Region:
    """[start,end) 向两侧扩到最近的分隔符（各最多 _SPAN_RADIUS 字），右侧带上句末标点。"""
    lo, floor = start, max(0, start - _SPAN_RADIUS)
    while lo > floor and text[lo - 1] not in ends:
        lo -= 1
    hi, ceiling = end, min(len(text), end + _SPAN_RADIUS)
    while hi < ceiling and text[hi] not in ends:
        hi += 1
    if hi < len(text) and text[hi] in ends and not text[hi].isspace():
        hi += 1
    return lo, hi


def _sentence(text: str, start: int, end: int, english: bool) -> str:
    """证据句：名字所在的整句，是草稿的原样子串。"""
    lo, hi = _bounds(text, start, end, _EN_SENTENCE_END if english else _ZH_SENTENCE_END)
    return text[lo:hi].strip()


def _sentence_uses(text: str, start: int, end: int, english: bool) -> bool:
    """名字所在分句里有"使用"类动词，且动词前没有否定词。

    只看分句（逗号/分号切开），避免"想起血遁术，转身催动青冥剑"把血遁术也算成用过。
    """
    ends = (_EN_SENTENCE_END if english else _ZH_SENTENCE_END) + _CLAUSE_SEPARATORS
    lo, hi = _bounds(text, start, end, ends)
    clause = text[lo:hi]
    for m in _EN_USE_VERB.finditer(clause):
        if not _EN_NEGATION.search(clause[max(0, m.start() - 24):m.start()]):
            return True
    if english:
        return False
    for verb in _ZH_USE_VERBS:
        pos = clause.find(verb)
        while pos >= 0:
            before = clause[max(0, pos - 2):pos]
            if not any(neg in before for neg in _ZH_NEGATION):
                return True
            pos = clause.find(verb, pos + len(verb))
    return False


def _cost_keywords(cost: Any) -> list[str]:
    """cost_json → 正文可检索的代价词（中文剥掉动词/数量前缀，英文取实词）。"""
    leaves: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if isinstance(key, str) and key.strip().lower() not in _COST_SCHEMA_KEYS:
                    leaves.append(key)
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            leaves.append(value)

    walk(cost)
    keywords: list[str] = []
    for leaf in leaves:
        if _CJK.search(leaf):
            candidates = [
                term for segment in re.split(r"[^\u3400-\u9fff]+", leaf)
                for term in _zh_cost_terms(segment)
            ]
        else:
            candidates = [
                word.lower().strip("'-") for word in re.findall(r"[A-Za-z][A-Za-z'\-]+", leaf)
            ]
            candidates = [w for w in candidates if len(w) >= 4 and w not in _EN_STOPWORDS]
        for kw in candidates:
            if kw not in keywords:
                keywords.append(kw)
    return keywords


def _zh_cost_terms(segment: str) -> list[str]:
    """"消耗十年寿元" → ["消耗十年寿元", "寿元"]；"灵力耗尽" → ["灵力耗尽", "灵力"]。"""
    segment = segment.strip()
    if len(segment) < 2:
        return []
    core = segment
    for _ in range(4):
        stripped = _ZH_COST_VERB_PREFIX.sub("", core)
        stripped = _ZH_COST_RESULT_SUFFIX.sub("", stripped)
        match = _ZH_QUANTITY.match(stripped)
        if match:
            stripped = stripped[match.end():]
        if stripped == core:
            break
        core = stripped
    terms: list[str] = []
    for part in [segment, *_ZH_QUANTITY.split(core)]:
        part = part.strip()
        if 2 <= len(part) <= 8 and not _ZH_QUANTITY.fullmatch(part) and part not in terms:
            terms.append(part)
    return terms


def _contains_keyword(text: str, keyword: str) -> bool:
    if _CJK.search(keyword):
        return keyword in text
    stem = keyword
    if stem.endswith("ies"):
        stem = stem[:-3]
    elif stem.endswith("es") and len(stem) > 4:
        stem = stem[:-2]
    elif stem.endswith("s") and not stem.endswith("ss"):
        stem = stem[:-1]
    if len(stem) > 4 and stem[-1] in "ey":
        stem = stem[:-1]
    return re.search(r"\b" + re.escape(stem) + r"\w*", text, re.IGNORECASE) is not None


# ── 通用小工具 ──────────────────────────────────────────────────────────────

def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_json(value: Any) -> Any:
    """JSON 字符串解析为对象；非 JSON 的自由文本原样保留（"消耗寿元" 也是代价描述）。"""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text
    return value


def _non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_non_empty(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_non_empty(v) for v in value)
    return True


def _unique(values: Iterable[Any]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in out:
            out.append(value)
    return tuple(out)


def _issue(check: str, span: str, scope: str, detail: str, fix: str) -> dict:
    return {
        "check": check,
        "severity": "warn",
        "detail": detail,
        "span": span,
        "fix": fix,
        "repair_scope": scope,
        "category": _CATEGORY_PREFIX + check,
    }


# ── 双语文案 ────────────────────────────────────────────────────────────────

_ZH_KIND = {"character": "人物", "item": "道具", "faction": "势力", "concept": "设定"}
_EN_KIND = {"character": "character", "item": "item", "faction": "faction", "concept": "element"}


def _newcomer_text(entity: _Entity, debut_in_climax: bool, english: bool) -> tuple[str, str]:
    name = entity.name
    if english:
        kind = _EN_KIND.get(entity.entity_type, "entity")
        detail = (
            f"The climax is resolved by {name}, a {kind} who first appears in this chapter "
            f"(deus ex machina): readers have never met {name} before"
            + (", and the debut happens inside the climax itself." if debut_in_climax else ".")
        )
        fix = (
            f"Resolve the climax with people, objects or abilities readers already know. "
            f"If {name} must act, introduce {name} in an earlier chapter and show what they can "
            f"do, or let the protagonist make the decisive move."
        )
        return detail, fix
    kind = _ZH_KIND.get(entity.entity_type, "实体")
    detail = (
        f"高潮由本章新登场的「{name}」解决（天降外挂）：该{kind}此前章节从未出现，读者无从预期"
        + ("，且首次出场就在高潮段。" if debut_in_climax else "。")
    )
    fix = (
        f"让高潮依靠读者已熟悉的人物、道具或能力来解决；若必须由「{name}」出手，"
        f"请在前文章节先让其登场并展示作用，或让主角承担关键的解决动作。"
    )
    return detail, fix


def _undemonstrated_text(name: str, new_now: bool, soft: bool, english: bool) -> tuple[str, str]:
    if english:
        reason = ("is first established in this chapter" if new_now
                  else "has never been shown or used before")
        if soft:
            return (
                f"Note: {name} {reason} and appears in the climax. Soft magic may stay "
                f"mysterious, but the more it solves the core conflict, the more it reads as an "
                f"authorial cheat.",
                f"Let the characters' choices, sacrifices or known abilities carry the "
                f"resolution and keep {name} in a supporting role, or give readers a glimpse of "
                f"it earlier.",
            )
        return (
            f"Ability used to resolve the conflict without setup: {name} {reason}, yet it "
            f"resolves the climax here (an ability can solve only as much conflict as readers "
            f"understand).",
            f"Show {name} in an earlier chapter, in a small test that makes clear what it can "
            f"do, what it cannot do and what it costs, or resolve the conflict with an ability "
            f"readers have already seen.",
        )
    reason = "本章才首次设定" if new_now else "此前从未展示或使用过"
    if soft:
        return (
            f"提示：「{name}」{reason}就出现在高潮段。软魔法体系可以保留神秘感，"
            f"但它越是解决核心冲突，读者越会觉得是作者在开外挂。",
            f"让人物的选择、牺牲或已知能力承担解决冲突的主要分量，「{name}」只作辅助；"
            f"或在前文先露出它的一角。",
        )
    return (
        f"能力未经铺垫即用于解决冲突：「{name}」{reason}，却在本章高潮段被用来解决问题"
        f"（读者理解多少，它才能解决多少冲突）。",
        f"在此前章节安排一次「{name}」的展示或小规模试用（交代它能做什么、不能做什么、"
        f"代价是什么），或改用读者已见识过的能力解决冲突。",
    )


def _unpaid_text(name: str, keywords: list[str], english: bool) -> tuple[str, str]:
    if english:
        cost = ", ".join(keywords)
        return (
            f"Unpaid cost: using {name} has a defined cost ({cost}), but the chapter never "
            f"shows it being paid.",
            f"After {name} is used, show the cost landing ({cost}) and let it constrain the "
            f"character's next choices; limitations are more interesting than powers.",
        )
    cost = "、".join(keywords)
    return (
        f"代价未付：「{name}」的使用代价（{cost}）未在正文兑现。",
        f"在使用「{name}」之后写出代价的兑现（{cost}），并让它影响人物后续的选择与行动"
        f"——限制比能力本身更有戏。",
    )


def _late_text(name: str, progress: float, threshold: float, english: bool) -> tuple[str, str]:
    tail = max(1, round((1.0 - threshold) * 100))
    pct = round(progress * 100)
    if english:
        return (
            f"Avoid new characters or setting elements in the last {tail}% of a volume: "
            f"{name} first appears in this chapter ({pct}% through the volume).",
            f"Give this role to characters or elements already on the page, or plant {name} "
            f"earlier in the volume.",
        )
    return (
        f"卷末 {tail}% 不宜引入新人物/新设定：「{name}」在本章（卷进度 {pct}%）才首次出现。",
        f"卷末宜收束已有线索：改由已登场的人物/设定承担这一功能，或把「{name}」提前到本卷前段铺垫。",
    )


def _ratio_text(new_count: int, old_uses: int, names: list[str], english: bool) -> tuple[str, str]:
    if english:
        return (
            f"Expand before you add: this volume has introduced {new_count} new abilities "
            f"(including {', '.join(names)} in this chapter) but only {old_uses} new uses of "
            f"existing ones.",
            "Find new applications, combinations and limits of existing abilities before adding "
            "more, and foreshadow any new ability earlier.",
        )
    return (
        f"先扩展已有能力，再新增：本卷已新增 {new_count} 项能力/金手指（含本章的"
        f"「{'」「'.join(names)}」），而旧能力在本卷的新用法只有 {old_uses} 次。",
        "先挖掘已有能力的新用法、组合与限制（用一个 A 带出 BCD），再考虑新增；"
        "新能力出场前先在前文铺垫。",
    )
