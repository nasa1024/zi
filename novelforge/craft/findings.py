"""Finding 统一结构（P1#8，oh-story Findings Schema + inkos repair_scope）。

{"severity": "block|warn", "category": "...", "evidence": "草稿原文片段",
 "issue": "...", "fix": "...", "repair_scope": "local|structural", "source": "..."}

LLM 来源（llm_soft/craft_llm）的 finding：evidence 必须是草稿子串（空白归一后），
否则整条丢弃——「无证据不输出」。validator/craft 确定性来源不强制 evidence。
"""
from __future__ import annotations

import re

_VALID_SEVERITY = {"block", "warn"}
_VALID_SCOPE = {"local", "structural"}
_LLM_SOURCES = {"llm_soft", "craft_llm"}


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def normalize_findings(raw: list, draft_text: str, source: str) -> list[dict]:
    """逐字段宽容归一：畸形字段丢该条不崩整轮；旧字段名兼容映射。"""
    draft_norm = _norm_ws(draft_text)
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        issue = str(item.get("issue") or item.get("desc") or item.get("detail") or "").strip()
        if not issue:
            continue
        evidence = str(item.get("evidence") or item.get("span") or "").strip()
        if source in _LLM_SOURCES and (not evidence or _norm_ws(evidence) not in draft_norm):
            continue   # 无证据不输出
        severity = item.get("severity")
        if severity not in _VALID_SEVERITY:
            severity = "warn"
        scope = item.get("repair_scope")
        if scope not in _VALID_SCOPE:
            scope = "local"
        category = str(item.get("category") or item.get("subclass")
                       or item.get("check") or "general")
        out.append({
            "severity": severity, "category": category,
            "evidence": evidence[:300], "issue": issue[:500],
            "fix": str(item.get("fix") or "")[:200],
            "repair_scope": scope, "source": source,
        })
    return out


def findings_to_issues_str(findings: list[dict]) -> str:
    """补丁/重写 prompt 的问题清单：issue + 原文证据 + 修改建议三行体。

    evidence 是锚点补丁 find 字段的天然锚点；兼容旧 issue 形（check/detail/span/desc）。
    """
    lines: list[str] = []
    for f in findings:
        cat = f.get("category") or f.get("check") or f.get("subclass") or "?"
        issue = f.get("issue") or f.get("desc") or f.get("detail") or str(f)
        lines.append(f"- [{cat}] {issue}")
        ev = f.get("evidence") or f.get("span")
        if ev:
            lines.append(f"  原文：「{ev}」")
        if f.get("fix"):
            lines.append(f"  建议：{f['fix']}")
    return "\n".join(lines)
