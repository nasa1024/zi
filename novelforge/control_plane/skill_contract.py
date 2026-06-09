"""SkillContract：skill 的静态元数据（§07.2）。

Contract 是纯数据描述，不包含执行逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .llm.tiers import ModelTier


class SkillTrigger(str, Enum):
    CHAPTER_START = "chapter_start"
    CHAPTER_DRAFT = "chapter_draft"
    CONTINUITY_CHECK = "continuity_check"
    CRAFT_CHECK = "craft_check"
    REVISE = "revise"
    MANUAL = "manual"


@dataclass
class IOSpec:
    """输入/输出字段描述（文档用，运行时不强制类型）。"""
    fields: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class DoDCheck:
    """Definition-of-Done 检查项。"""
    name: str
    description: str
    required: bool = True


@dataclass
class SkillContract:
    name: str
    version: str
    trigger: SkillTrigger
    model_tier: ModelTier
    inputs: IOSpec = field(default_factory=IOSpec)
    outputs: IOSpec = field(default_factory=IOSpec)
    dod: list[DoDCheck] = field(default_factory=list)
    read_scopes: list[str] = field(default_factory=list)   # DB 表或逻辑域
    write_scopes: list[str] = field(default_factory=list)
    cache_prefix_keys: list[str] = field(default_factory=list)
    description: Optional[str] = None
    timeout_seconds: int = 120
