"""v22/F4：分维度质量门控（WSE-bench / LongJudgeBench：单一加权总分不能代表质量）。

``evaluate_quality(score, dimensions, qcfg)`` 是全项目唯一的"低质量"判定口径，
供以下调用点共用，保证润色触发、autopilot 降级计数与看板统计一致：

- ``Orchestrator._quality_pass``：决定是否润色、润色稿是否保留；
- ``app/autopilot_manager``：连续低质量计入降级；
- ``app/api/orchestrator.pipeline_stats``：``low_quality_count``。

门控模式（``QualityConfig.gate_mode``）：

- ``"dimensions"``（默认）：任一"有下限且有分"的维度低于 ``dimension_min[dim]``
  即低分，总分只作展示；评分没有可校验的维度时回落为 ``score < min_score``；
- ``"total"``：旧口径 ``score < min_score``；
- 未知取值按 ``"total"`` 处理（配置拼错时退回旧行为，而不是静默放行）。

纯函数、确定性、无 IO。``qcfg`` 既可以是 ``QualityConfig``，也可以是
``verdict_detail()`` 落库的 dict（看板/换稿按当时的阈值复算）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

GATE_DIMENSIONS = "dimensions"
GATE_TOTAL = "total"

# QualityVerdict.mode：实际采用的判定依据
MODE_DIMENSIONS = "dimensions"          # 按维度下限判定
MODE_TOTAL = "total"                    # 配置为总分口径
MODE_TOTAL_FALLBACK = "total_fallback"  # 配置为维度口径，但无可校验维度 → 总分兜底
MODE_UNSCORED = "unscored"              # 既无总分也无维度：不判低分

_DEFAULT_MIN_SCORE = 6.0


@dataclass(frozen=True)
class QualityVerdict:
    low: bool
    reasons: list[str] = field(default_factory=list)
    mode: str = MODE_UNSCORED

    def to_dict(self) -> dict[str, Any]:
        return {"low": self.low, "reasons": list(self.reasons), "mode": self.mode}


def _cfg(qcfg: Any, name: str, default: Any) -> Any:
    if isinstance(qcfg, Mapping):
        return qcfg.get(name, default)
    return getattr(qcfg, name, default)


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _fmt(value: float) -> str:
    return f"{round(value, 2):g}"


def gate_mode(qcfg: Any) -> str:
    mode = str(_cfg(qcfg, "gate_mode", GATE_DIMENSIONS) or GATE_DIMENSIONS).strip().lower()
    return mode if mode in (GATE_DIMENSIONS, GATE_TOTAL) else GATE_TOTAL


def dimension_thresholds(qcfg: Any) -> dict[str, float]:
    raw = _cfg(qcfg, "dimension_min", None)
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, float] = {}
    for dim, threshold in raw.items():
        value = _num(threshold)
        if value is not None:
            out[str(dim)] = value
    return out


def evaluate_quality(
    score: Optional[float],
    dimensions: Optional[Mapping[str, Any]],
    qcfg: Any,
) -> QualityVerdict:
    """按 ``qcfg.gate_mode`` 判定本章是否低质量。

    ``reasons`` 为未过线项，形如 ``"hook 4<5"`` / ``"score 5.5<6"``（语言无关，
    便于看板展示与日志检索）。
    """
    mode = gate_mode(qcfg)
    if mode == GATE_DIMENSIONS:
        thresholds = dimension_thresholds(qcfg)
        dims = dimensions if isinstance(dimensions, Mapping) else {}
        checked: list[tuple[str, float, float]] = []
        for dim, threshold in thresholds.items():
            value = _num(dims.get(dim))
            if value is not None:
                checked.append((dim, value, threshold))
        if checked:
            reasons = [
                f"{dim} {_fmt(value)}<{_fmt(threshold)}"
                for dim, value, threshold in checked if value < threshold
            ]
            return QualityVerdict(low=bool(reasons), reasons=reasons, mode=MODE_DIMENSIONS)
    total = _num(score)
    if total is None:
        return QualityVerdict(low=False, reasons=[], mode=MODE_UNSCORED)
    min_score = _num(_cfg(qcfg, "min_score", _DEFAULT_MIN_SCORE))
    if min_score is None:
        min_score = _DEFAULT_MIN_SCORE
    low = total < min_score
    return QualityVerdict(
        low=low,
        reasons=[f"score {_fmt(total)}<{_fmt(min_score)}"] if low else [],
        mode=MODE_TOTAL if mode == GATE_TOTAL else MODE_TOTAL_FALLBACK,
    )


def verdict_detail(verdict: QualityVerdict, qcfg: Any) -> dict[str, Any]:
    """落库用：判定结果 + 当时的阈值（看板/换稿据此复算，不受默认值漂移影响）。"""
    min_score = _num(_cfg(qcfg, "min_score", _DEFAULT_MIN_SCORE))
    return {
        **verdict.to_dict(),
        "gate_mode": gate_mode(qcfg),
        "min_score": _DEFAULT_MIN_SCORE if min_score is None else min_score,
        "dimension_min": dimension_thresholds(qcfg),
    }


def is_improvement(
    old_score: Optional[float],
    old: QualityVerdict,
    new_score: Optional[float],
    new: QualityVerdict,
) -> bool:
    """润色稿是否保留：未过线项更少者胜；持平时总分不降即保留（旧行为）。

    纯总分口径下两者的 reasons 至多 1 项，因此等价于旧规则 ``new >= old``；
    维度口径下修好了低分维度、总分略降的润色稿不再被误回退。
    """
    if new_score is None:
        return False
    if len(new.reasons) != len(old.reasons):
        return len(new.reasons) < len(old.reasons)
    if old_score is None:
        return True
    return new_score >= old_score
