"""SkillContext / SkillResult / Skill Protocol（§07.3）。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from .llm.gateway import LLMGateway
from .skill_contract import SkillContract


@dataclass
class SkillContext:
    project_id: str
    target_chapter: int
    mode: str                           # "human_gate" | "auto_promote" | "hybrid"
    as_of_chapter: int                  # 用于 get_world_state 的时间截面
    budget: Any                         # BudgetLedger
    llm: LLMGateway
    conn: sqlite3.Connection
    workspace: dict = field(default_factory=dict)  # 章内共享黑板
    extra: dict = field(default_factory=dict)      # skill 自定义扩展


@dataclass
class DoDOutcome:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class SkillResult:
    skill_name: str
    ok: bool
    payload: dict = field(default_factory=dict)      # skill 输出数据
    dod_outcomes: list[DoDOutcome] = field(default_factory=list)
    error: Optional[str] = None
    usage_summary: Optional[str] = None


@runtime_checkable
class Skill(Protocol):
    contract: SkillContract

    def run(self, ctx: SkillContext) -> SkillResult: ...
