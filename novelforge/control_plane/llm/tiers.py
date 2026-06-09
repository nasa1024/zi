"""模型层级枚举（FAST/MID/STRONG 与 HAIKU/SONNET/OPUS 双命名）。"""
from enum import Enum


class ModelTier(str, Enum):
    FAST = "fast"
    MID = "mid"
    STRONG = "strong"


# HAIKU / SONNET / OPUS 是别名，直接引用 Enum 成员
HAIKU = ModelTier.FAST
SONNET = ModelTier.MID
OPUS = ModelTier.STRONG
