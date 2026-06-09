"""PacingController：爽点节拍控制（§05.3-05.4）。

读写 pacing_cursor 单行表，给 PlannerSkill 提供节拍建议。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional


@dataclass
class PacingState:
    chapters_since_big_payoff: int = 0
    kchars_since_small_payoff: float = 0.0
    buildup: int = 0
    recent_high_streak: int = 0

    @property
    def needs_small_payoff(self) -> bool:
        return self.kchars_since_small_payoff >= 3.0   # 每 3k 字一次小爽点

    @property
    def needs_big_payoff(self) -> bool:
        return self.chapters_since_big_payoff >= 10    # 每卷（约10章）一次大爽点

    @property
    def needs_cooldown(self) -> bool:
        return self.recent_high_streak >= 3             # 连续3章高潮要回落防麻木


class PacingController:
    def get_state(self, conn: sqlite3.Connection) -> PacingState:
        row = conn.execute("SELECT * FROM pacing_cursor WHERE id=1").fetchone()
        if row is None:
            return PacingState()
        return PacingState(
            chapters_since_big_payoff=row["chapters_since_big_payoff"],
            kchars_since_small_payoff=row["kchars_since_small_payoff"],
            buildup=row["buildup"],
            recent_high_streak=row["recent_high_streak"],
        )

    def update(
        self,
        chapter: int,
        beats: list[dict],
        draft_chars: int,
        conn: sqlite3.Connection,
    ) -> PacingState:
        """章节完成后更新 pacing_cursor。"""
        state = self.get_state(conn)

        has_big_payoff = any(b.get("beat_type") == "payoff_beat" for b in beats)
        has_small_payoff = any(
            b.get("beat_type") in ("turn", "payoff_beat") for b in beats
        )
        is_high_tension = any(
            b.get("beat_type") in ("tension_point", "payoff_beat") for b in beats
        )

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

        # 连续高潮计数
        if is_high_tension:
            state.recent_high_streak += 1
        else:
            state.recent_high_streak = 0

        _upsert_cursor(conn, state)
        return state

    def recommend_beat_hint(self, state: PacingState) -> str:
        """给 PlannerSkill 的节拍建议（附加到 chapter_goal 里）。"""
        if state.needs_cooldown:
            return "节奏提示：前几章张力较高，本章应适当回落，加入生活化场景或人物内心戏。"
        if state.needs_big_payoff and state.buildup >= 5:
            return "节奏提示：蓄力充足，本章应包含一个大爽点（payoff_beat），兑现前期铺垫。"
        if state.needs_small_payoff:
            return "节奏提示：距离上次小爽点已超过 3000 字，本章需要至少一个 turn 或 payoff_beat。"
        return ""


def _upsert_cursor(conn: sqlite3.Connection, state: PacingState) -> None:
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
