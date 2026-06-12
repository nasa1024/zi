"""钩子类型枚举与宽容归一（P1#7，oh-story 章首 7 式 / 章尾 13 式）。

枚举 key 用英文存库；LLM/人工输入经 normalize_hook_type 归一，
匹配不上存 'other'——other 不参与「连续两章同型钩子」检查。
存库不加 CHECK 约束：归一函数是唯一入口，约束放代码层。
"""
from __future__ import annotations

from typing import Optional

# key → (中文名, 关键词表)。关键词按"包含"匹配。
OPENING_HOOK_TYPES: dict[str, tuple[str, list[str]]] = {
    "suspense":  ("悬念", ["悬念", "谜团开局", "疑问"]),
    "conflict":  ("冲突", ["冲突", "对峙", "争执"]),
    "dialogue":  ("对话切入", ["对话切入", "对话开场", "对白"]),
    "action":    ("动作", ["动作", "打斗", "战斗开场", "追逐"]),
    "anomaly":   ("反常", ["反常", "异常", "诡异"]),
    "crisis":    ("危机", ["危机", "命悬", "险境", "绝境"]),
    "flashback": ("倒叙", ["倒叙", "回忆切入", "闪回"]),
}

ENDING_HOOK_TYPES: dict[str, tuple[str, list[str]]] = {
    "cliffhanger":  ("命悬一线", ["命悬一线", "命悬", "千钧一发", "生死关头"]),
    "reversal":     ("反转", ["反转", "逆转", "翻转"]),
    "reveal":       ("揭秘", ["揭秘", "真相", "揭露", "身份曝光"]),
    "new_threat":   ("新威胁", ["新威胁", "强敌", "大敌", "威胁逼近"]),
    "mystery":      ("新谜团", ["新谜团", "谜团", "疑团", "未知"]),
    "promise":      ("承诺约战", ["约战", "承诺", "赌约", "之约"]),
    "arrival":      ("神秘登场", ["神秘人", "登场", "现身", "驾到"]),
    "decision":     ("重大抉择", ["抉择", "选择", "两难"]),
    "countdown":    ("倒计时", ["倒计时", "期限", "时限", "大限"]),
    "loss":         ("失去代价", ["失去", "代价", "牺牲", "陨落"]),
    "power_tease":  ("力量预告", ["力量预告", "觉醒前兆", "突破在即", "异变"]),
    "relationship": ("关系变化", ["关系", "决裂", "结盟", "告白"]),
    "humiliation":  ("受辱蓄势", ["受辱", "羞辱", "蓄势", "隐忍"]),
}


def _enum_for(kind: str) -> dict[str, tuple[str, list[str]]]:
    return OPENING_HOOK_TYPES if kind == "opening" else ENDING_HOOK_TYPES


def normalize_hook_type(raw: Optional[str], kind: str) -> str:
    """归一钩子类型：精确 key → 中文名/关键词包含 → 'other'。kind: opening|ending。"""
    if not raw or not isinstance(raw, str):
        return "other"
    enum = _enum_for(kind)
    text = raw.strip().lower()
    if text in enum:
        return text
    for key, (cn, keywords) in enum.items():
        if cn in raw or any(k in raw for k in keywords):
            return key
    return "other"


def hook_label(key: Optional[str]) -> str:
    """枚举 key → 中文名（用于 prompt/前端展示）；other/未知原样返回。"""
    if not key:
        return "other"
    for enum in (OPENING_HOOK_TYPES, ENDING_HOOK_TYPES):
        if key in enum:
            return enum[key][0]
    return key


def hook_menu(kind: str) -> str:
    """给 prompt 用的枚举菜单：'suspense(悬念)/conflict(冲突)/…'。"""
    enum = _enum_for(kind)
    return "/".join(f"{k}({v[0]})" for k, v in enum.items())
