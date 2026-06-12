"""NovelForge skills 包。

使用前用 register_default_skills(registry) 注册到 SkillRegistry。
"""
from .chapter_draft_skill import ChapterDraftSkill
from .cold_extract_skill import ColdExtractSkill
from .continuity_check_skill import ContinuityCheckSkill
from .craft_check_skill import CraftCheckSkill
from .planner_skill import PlannerSkill
from .volume_plan_skill import VolumePlanSkill


def register_default_skills(registry=None):
    """将默认 skill 注册到 registry；缺省使用全局注册表。"""
    from ..control_plane.skill_registry import get_registry
    reg = registry or get_registry()
    reg.register(PlannerSkill())
    reg.register(ChapterDraftSkill())
    reg.register(ContinuityCheckSkill())
    reg.register(CraftCheckSkill())
    reg.register(ColdExtractSkill())
    reg.register(VolumePlanSkill())
    return reg


__all__ = [
    "PlannerSkill",
    "ChapterDraftSkill",
    "ColdExtractSkill",
    "ContinuityCheckSkill",
    "CraftCheckSkill",
    "VolumePlanSkill",
    "register_default_skills",
]
