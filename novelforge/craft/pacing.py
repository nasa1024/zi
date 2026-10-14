"""PacingController：爽点节拍控制（§05.3-05.4）。

读写 pacing_cursor 单行表，给 PlannerSkill 提供节拍建议。

v22/F3 极性序列：``low_streak`` / ``polarity_streak`` / ``last_polarity`` 三列由
``craft/polarity.settle`` 按 ``pacing_state`` 逐章历史**重算**写入（重写章节也安全）；
``update`` 只维护旧的累积计数，``_upsert_cursor`` 不触碰这三列。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from ..config import PolarityConfig

_POLARITY_VALUES = ("+", "-", "±")
# 历史扫描上限：连续计数超过此值已无区别，避免对超长篇全表扫描。
_STREAK_SCAN_LIMIT = 200


@dataclass
class PacingState:
    chapters_since_big_payoff: int = 0
    kchars_since_small_payoff: float = 0.0
    buildup: int = 0
    recent_high_streak: int = 0
    # v22/F3：截至最新已结算章的极性序列（见 recompute_polarity_streaks）
    low_streak: int = 0                    # 连续低谷（转向为 -）章数
    polarity_streak: int = 0               # 与 last_polarity 同向的连续章数
    last_polarity: Optional[str] = None    # 最新已结算章的实际转向 + / - / ±
    # needs_turn 的阈值；get_state(max_low_streak=…) 可覆盖，默认取 PolarityConfig
    max_low_streak: int = PolarityConfig.max_low_streak

    @property
    def needs_small_payoff(self) -> bool:
        return self.kchars_since_small_payoff >= 3.0   # 每 3k 字一次小爽点

    @property
    def needs_big_payoff(self) -> bool:
        return self.chapters_since_big_payoff >= 10    # 每卷（约10章）一次大爽点

    @property
    def needs_cooldown(self) -> bool:
        return self.recent_high_streak >= 3             # 连续3章高潮要回落防麻木

    @property
    def needs_turn(self) -> bool:
        """连续低谷已达上限：下一章必须安排转机（千幻冰云"最惨不能惨过三章"）。"""
        return self.max_low_streak > 0 and self.low_streak >= self.max_low_streak


class PacingController:
    def get_state(
        self,
        conn: sqlite3.Connection,
        *,
        max_low_streak: int | None = None,
    ) -> PacingState:
        limit = (
            max(1, int(max_low_streak)) if max_low_streak is not None
            else PolarityConfig.max_low_streak
        )
        row = conn.execute("SELECT * FROM pacing_cursor WHERE id=1").fetchone()
        if row is None:
            return PacingState(max_low_streak=limit)
        keys = set(row.keys())   # v22 之前的库没有极性列 → 取默认值

        def _int(name: str) -> int:
            return int(row[name] or 0) if name in keys else 0

        last = row["last_polarity"] if "last_polarity" in keys else None
        return PacingState(
            chapters_since_big_payoff=row["chapters_since_big_payoff"],
            kchars_since_small_payoff=row["kchars_since_small_payoff"],
            buildup=row["buildup"],
            recent_high_streak=row["recent_high_streak"],
            low_streak=_int("low_streak"),
            polarity_streak=_int("polarity_streak"),
            last_polarity=last if last in _POLARITY_VALUES else None,
            max_low_streak=limit,
        )

    def update(
        self,
        chapter: int,
        beats: list[dict],
        draft_chars: int,
        conn: sqlite3.Connection,
        *,
        actuals=None,
        contract: dict | None = None,
    ) -> PacingState:
        """章节完成后更新 pacing_cursor。

        v22：``actuals``（ChapterActuals，含实际 polarity）与 ``contract``（本章契约，
        含计划 polarity/turning_point）为兼容保留的可选参数——极性计数不在这里累加，
        而由 ``craft/polarity.settle`` 写入逐章 ``pacing_state`` 后重算（幂等）。
        ``aftermath`` beat 是高潮后的主动平复：本章出现余韵即视为冷却，连续高潮清零。
        """
        state = self.get_state(conn)

        has_big_payoff = any(b.get("beat_type") == "payoff_beat" for b in beats)
        has_small_payoff = any(
            b.get("beat_type") in ("turn", "payoff_beat") for b in beats
        )
        is_high_tension = any(
            b.get("beat_type") in ("tension_point", "payoff_beat") for b in beats
        )
        has_aftermath = any(b.get("beat_type") == "aftermath" for b in beats)

        # 大爽点计数
        if has_big_payoff:
            state.chapters_since_big_payoff = 0
            state.buildup = 0
        else:
            state.chapters_since_big_payoff += 1
            state.buildup = min(state.buildup + 1, 20)

        # 小爽点 kchars 计数
        if has_small_payoff:
            state.kchars_since_small_payoff = 0.0
        else:
            state.kchars_since_small_payoff += draft_chars / 1000.0

        # 连续高潮计数（余韵 = 冷却：猫腻"小高潮、平复期和余韵"）
        if has_aftermath:
            state.recent_high_streak = 0
        elif is_high_tension:
            state.recent_high_streak += 1
        else:
            state.recent_high_streak = 0

        _upsert_cursor(conn, state)
        return state

    def recompute_polarity_streaks(self, conn: sqlite3.Connection) -> dict:
        """按 ``pacing_state`` 逐章历史重算极性计数并写入 pacing_cursor（不提交）。

        截至最新一章：``last_polarity`` = 该章实际转向；``polarity_streak`` = 末尾与之
        同向的连续章数；``low_streak`` = 末尾连续 ``-`` 章数（± 视为转折，打断低谷）。
        章号不连续或转向未知（NULL）都会打断连续计数——宁可少报，不误报。
        纯重算、不累加，因此同一章重写/换稿后重复结算结果不变。
        """
        rows = conn.execute(
            "SELECT chapter, polarity FROM pacing_state ORDER BY chapter DESC LIMIT ?",
            (_STREAK_SCAN_LIMIT,),
        ).fetchall()
        trail = trailing_polarities([(r["chapter"], r["polarity"]) for r in rows])
        last = trail[0] if trail else None
        polarity_streak = leading_run(trail, last)
        low_streak = polarity_streak if last == "-" else 0
        conn.execute(
            "INSERT INTO pacing_cursor(id, low_streak, polarity_streak, last_polarity,"
            "  updated_at) VALUES(1,?,?,?,datetime('now'))"
            " ON CONFLICT(id) DO UPDATE SET"
            "   low_streak=excluded.low_streak,"
            "   polarity_streak=excluded.polarity_streak,"
            "   last_polarity=excluded.last_polarity,"
            "   updated_at=excluded.updated_at",
            (low_streak, polarity_streak, last),
        )
        return {
            "low_streak": low_streak,
            "polarity_streak": polarity_streak,
            "last_polarity": last,
        }

    def recommend_beat_hint(self, state: PacingState) -> str:
        """给 PlannerSkill 的节拍建议（附加到 chapter_goal 里）。

        v22：连续低谷达到上限时，转机要求排在最前（可与下面的单条建议同时出现）。
        """
        turn = ""
        if state.needs_turn:
            turn = (
                f"节奏提示：已连续 {state.low_streak} 章低谷（转向为 -），本章必须安排转机，"
                "让局面向主角目标好转（转向 + 或 ±）。"
            )
        return "\n".join(p for p in (turn, self._legacy_hint(state)) if p)

    @staticmethod
    def _legacy_hint(state: PacingState) -> str:
        if state.needs_cooldown:
            return "节奏提示：前几章张力较高，本章应适当回落，加入生活化场景或人物内心戏。"
        if state.needs_big_payoff and state.buildup >= 5:
            return "节奏提示：蓄力充足，本章应包含一个大爽点（payoff_beat），兑现前期铺垫。"
        if state.needs_small_payoff:
            return "节奏提示：距离上次小爽点已超过 3000 字，本章需要至少一个 turn 或 payoff_beat。"
        return ""


def trailing_polarities(rows: list[tuple[int, Optional[str]]]) -> list[Optional[str]]:
    """``rows`` 按章号降序；返回从首行起章号连续的转向序列（未知转向记为 None）。"""
    out: list[Optional[str]] = []
    expected: Optional[int] = None
    for chapter, polarity in rows:
        if expected is not None and chapter != expected:
            break
        out.append(polarity if polarity in _POLARITY_VALUES else None)
        expected = chapter - 1
    return out


def leading_run(seq: list[Optional[str]], value: Optional[str]) -> int:
    """序列开头连续等于 ``value`` 的个数（``value`` 为 None 时恒为 0）。"""
    if value is None:
        return 0
    n = 0
    for item in seq:
        if item != value:
            break
        n += 1
    return n


def _upsert_cursor(conn: sqlite3.Connection, state: PacingState) -> None:
    # 只写旧的累积列：v22 极性列（low_streak/polarity_streak/last_polarity）由
    # polarity.settle 重算——冲突更新时保持原值，首次插入取列默认值。
    conn.execute(
        "INSERT INTO pacing_cursor(id, chapters_since_big_payoff, kchars_since_small_payoff,"
        "  buildup, recent_high_streak, updated_at)"
        " VALUES(1,?,?,?,?,datetime('now'))"
        " ON CONFLICT(id) DO UPDATE SET"
        "   chapters_since_big_payoff=excluded.chapters_since_big_payoff,"
        "   kchars_since_small_payoff=excluded.kchars_since_small_payoff,"
        "   buildup=excluded.buildup,"
        "   recent_high_streak=excluded.recent_high_streak,"
        "   updated_at=excluded.updated_at",
        (
            state.chapters_since_big_payoff,
            state.kchars_since_small_payoff,
            state.buildup,
            state.recent_high_streak,
        ),
    )
