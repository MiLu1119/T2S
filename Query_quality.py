"""Explainable runtime confidence signals for a completed Text-to-SQL request."""

from __future__ import annotations

from typing import Any


def assess_confidence(state: dict[str, Any], warning_threshold: float = 0.65) -> dict[str, Any]:
    """Return a heuristic score, not a calibrated model probability.

    The score deliberately combines observable Agent signals so users can see why
    a result is marked risky even when a provider does not expose token logprobs.
    """
    score = 0.94
    reasons: list[str] = []
    trace = dict(state.get("generation_trace", {}) or {})
    retries = int(state.get("retry_count", 0) or 0)
    if state.get("status") != "success":
        score -= 0.55
        reasons.append("查询未正常完成")
    if retries:
        penalty = min(0.30, retries * 0.12)
        score -= penalty
        reasons.append(f"SQL 经历 {retries} 次失败重写")
    if trace.get("etc_triggered"):
        score -= 0.12
        reasons.append("生成阶段出现上升的不确定性趋势")
    if not trace.get("logprobs_available"):
        score -= 0.06
        reasons.append("模型 API 未提供 token 概率，无法做熵校准")
    if state.get("execution_truncated"):
        score -= 0.08
        reasons.append("结果达到返回上限并被截断")
    if not state.get("execution_rows") and state.get("status") == "success":
        score -= 0.05
        reasons.append("查询成功但结果为空，建议复核筛选条件")
    cost = dict(state.get("cost_check", {}) or {})
    if cost.get("level") == "high":
        score -= 0.08
        reasons.append("执行计划成本较高")
    score = round(max(0.0, min(1.0, score)), 2)
    level = "high" if score >= 0.80 else "medium" if score >= warning_threshold else "low"
    return {
        "score": score,
        "level": level,
        "low_confidence": score < warning_threshold,
        "threshold": warning_threshold,
        "reasons": reasons or ["校验、执行和结果信号均正常"],
        "method": "heuristic_runtime_signals",
        "calibrated_probability": False,
    }
