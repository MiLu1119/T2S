"""
reward.py
---------
把 safety / executor / compare 串起来，输出 GRPO 训练用的标量 reward。

打分规则（讨论中定的第一版）：
    - 未通过 safety 检查                         -> SAFETY_PENALTY   (-1.0)
    - 通过 safety 但执行失败（语法错误/超时/字段不存在等） -> 0.0
    - 执行成功但结果与 ground truth 不匹配          -> EXECUTION_REWARD (+0.3)
    - 执行成功且结果与 ground truth 匹配            -> EXECUTION_REWARD + CORRECTNESS_REWARD (+1.3)

reward 分量单独保留在 RewardBreakdown 里（而不是只返回一个 float），
是为了训练时能把 execution_rate / correctness_rate / safety_violation_rate
分开画在 tensorboard 上，早期就能看出模型是不是在钻空子。
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, asdict
from typing import Optional

from Compare import compare_results
from Executor import execute_sql
from Safety import check_sql_safety

SAFETY_PENALTY = -1.0
EXECUTION_REWARD = 0.3
CORRECTNESS_REWARD = 1.0


@dataclass
class RewardBreakdown:
    total_reward: float
    passed_safety: bool
    executed_successfully: bool
    is_correct: bool
    error_type: Optional[str] = None
    elapsed_sec: float = 0.0
    detail: str = ""
    partial_similarity: float = 0.0
    partial_bonus: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def compute_reward(
    db_path: str,
    predicted_sql: str,
    ground_truth_sql: str,
    timeout_sec: float = 5.0,
    use_partial_reward: bool = False,
    partial_bonus_cap: float = 0.3,
    row_f1_weight: float = 0.3,
    value_f1_weight: float = 0.2,
    scalar_weight: float = 0.1,
) -> RewardBreakdown:
    """
    对一条 (predicted_sql, ground_truth_sql) pair 计算 reward。

    ground_truth_sql 会现场执行一次而不是用缓存好的结果，是为了处理
    BIRD 里 ground truth 依赖时间函数（如 CURRENT_DATE）或多次执行结果
    不同的极少数 edge case；如果训练时性能吃紧，可以在 dataloader 阶段
    把每条样本的 gt 结果预先算好缓存，这里改成接收 gt_rows 而不是 gt_sql。
    """
    # 1. Safety Gate —— 优先级最高，不安全直接拦截，不再执行
    safety_result = check_sql_safety(predicted_sql)
    if not safety_result.is_safe:
        return RewardBreakdown(
            total_reward=SAFETY_PENALTY,
            passed_safety=False,
            executed_successfully=False,
            is_correct=False,
            detail=f"safety violation: {safety_result.reason}",
        )

    # 2. 执行 predicted SQL
    pred_result = execute_sql(db_path, predicted_sql, timeout_sec=timeout_sec)
    if not pred_result.success:
        return RewardBreakdown(
            total_reward=0.0,
            passed_safety=True,
            executed_successfully=False,
            is_correct=False,
            error_type=pred_result.error_type,
            elapsed_sec=pred_result.elapsed_sec,
            detail=f"execution failed: {pred_result.error}",
        )

    # 3. 执行 ground truth SQL（正常不应该失败；如果失败说明数据本身有问题，
    #    不能怪模型，也不给 reward，同时应该在数据清洗阶段单独记录下来）
    gt_result = execute_sql(db_path, ground_truth_sql, timeout_sec=timeout_sec)
    if not gt_result.success:
        return RewardBreakdown(
            total_reward=EXECUTION_REWARD,
            passed_safety=True,
            executed_successfully=True,
            is_correct=False,
            detail=f"WARNING: ground truth SQL itself failed to execute: {gt_result.error}",
        )

    # 4. 结果比对（严格模式，对齐 BIRD 官方口径）
    is_correct = compare_results(pred_result.rows, gt_result.rows, strict=True)

    partial_scores = partial_similarity_scores(pred_result.rows, gt_result.rows)
    partial_similarity = max(
        partial_scores["row_f1"],
        partial_scores["value_f1"],
        partial_scores["scalar_similarity"],
    )
    partial_bonus = 0.0
    if use_partial_reward and not is_correct:
        partial_bonus = min(
            partial_bonus_cap,
            max(
                row_f1_weight * partial_scores["row_f1"],
                value_f1_weight * partial_scores["value_f1"],
                scalar_weight * partial_scores["scalar_similarity"],
            ),
        )

    total = EXECUTION_REWARD + (CORRECTNESS_REWARD if is_correct else partial_bonus)
    return RewardBreakdown(
        total_reward=total,
        passed_safety=True,
        executed_successfully=True,
        is_correct=is_correct,
        elapsed_sec=pred_result.elapsed_sec,
        detail="correct" if is_correct else "executed but result mismatch",
        partial_similarity=partial_similarity,
        partial_bonus=partial_bonus,
    )


def partial_similarity_scores(
    predicted_rows: list[tuple],
    ground_truth_rows: list[tuple],
) -> dict[str, float]:
    return {
        "row_f1": row_f1(predicted_rows, ground_truth_rows),
        "value_f1": value_f1(predicted_rows, ground_truth_rows),
        "scalar_similarity": scalar_similarity(predicted_rows, ground_truth_rows),
    }


def normalize_value(value):
    if isinstance(value, float):
        return round(value, 6)
    return value


def row_f1(predicted_rows: list[tuple], ground_truth_rows: list[tuple]) -> float:
    if not predicted_rows or not ground_truth_rows:
        return 0.0
    pred_set = {tuple(normalize_value(value) for value in row) for row in predicted_rows}
    gt_set = {tuple(normalize_value(value) for value in row) for row in ground_truth_rows}
    overlap = len(pred_set & gt_set)
    if overlap == 0:
        return 0.0
    precision = overlap / len(pred_set)
    recall = overlap / len(gt_set)
    return 2 * precision * recall / (precision + recall)


def value_f1(predicted_rows: list[tuple], ground_truth_rows: list[tuple]) -> float:
    pred_counter = Counter(flatten_values(predicted_rows))
    gt_counter = Counter(flatten_values(ground_truth_rows))
    if not pred_counter or not gt_counter:
        return 0.0
    overlap = sum((pred_counter & gt_counter).values())
    if overlap == 0:
        return 0.0
    precision = overlap / sum(pred_counter.values())
    recall = overlap / sum(gt_counter.values())
    return 2 * precision * recall / (precision + recall)


def flatten_values(rows: list[tuple]) -> list:
    values = []
    for row in rows:
        values.extend(normalize_value(value) for value in row)
    return values


def scalar_similarity(predicted_rows: list[tuple], ground_truth_rows: list[tuple]) -> float:
    if len(predicted_rows) != 1 or len(ground_truth_rows) != 1:
        return 0.0
    if len(predicted_rows[0]) != 1 or len(ground_truth_rows[0]) != 1:
        return 0.0
    pred_value = predicted_rows[0][0]
    gt_value = ground_truth_rows[0][0]
    if not isinstance(pred_value, (int, float)) or not isinstance(gt_value, (int, float)):
        return 0.0
    if pred_value == gt_value:
        return 1.0
    denom = max(abs(float(gt_value)), 1.0)
    rel_error = abs(float(pred_value) - float(gt_value)) / denom
    if not math.isfinite(rel_error):
        return 0.0
    return max(0.0, 1.0 - min(rel_error, 1.0))
