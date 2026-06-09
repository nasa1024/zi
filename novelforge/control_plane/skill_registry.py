"""SkillRegistry：注册 + 按名调用 skill（§07.4）。"""
from __future__ import annotations

from typing import Optional

from .skill_base import Skill, SkillContext, SkillResult


class SkillRegistry:
    def __init__(self) -> None:
        self._registry: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._registry[skill.contract.name] = skill

    def get(self, name: str) -> Optional[Skill]:
        return self._registry.get(name)

    def invoke(self, name: str, ctx: SkillContext) -> SkillResult:
        skill = self._registry.get(name)
        if skill is None:
            return SkillResult(
                skill_name=name, ok=False,
                error=f"skill '{name}' not registered",
            )
        return skill.run(ctx)

    def names(self) -> list[str]:
        return list(self._registry.keys())


# 全局默认注册表（可被覆盖）
_default_registry = SkillRegistry()


def get_registry() -> SkillRegistry:
    return _default_registry
