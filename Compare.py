"""
compare.py
----------
执行结果比对逻辑，对齐 BIRD 官方 evaluation 的口径，方便训练出来的
数字能和公开榜单/论文对比。

BIRD 官方实现本质就是：
    set(predicted_rows) == set(ground_truth_rows)
即：行顺序无关，但不做列顺序归一化、不做浮点容差。

这里额外提供一个 tolerant 模式（float 容差 + 可选列排序），
仅用于调试分析，训练用的 reward 默认走 strict 模式以保证口径一致。
"""

from __future__ import annotations

from typing import Any


def _normalize_value(v: Any, float_tol_ndigits: int | None) -> Any:
    """把浮点数按指定精度四舍五入，其他类型原样返回。"""
    if float_tol_ndigits is not None and isinstance(v, float):
        return round(v, float_tol_ndigits)
    return v


def _normalize_rows(
    rows: list[tuple[Any, ...]],
    float_tol_ndigits: int | None,
    sort_columns: bool,
) -> set[tuple[Any, ...]]:
    normalized = []
    for row in rows:
        vals = tuple(_normalize_value(v, float_tol_ndigits) for v in row)
        if sort_columns:
            # 列顺序不敏感：仅用于调试分析，不代表真实语义正确，
            # 因为不同列顺序理论上是不同的答案（除非 schema 保证列名唯一）。
            vals = tuple(sorted(vals, key=lambda x: (str(type(x)), x)))
        normalized.append(vals)
    return set(normalized)


def compare_results(
    predicted_rows: list[tuple[Any, ...]],
    ground_truth_rows: list[tuple[Any, ...]],
    strict: bool = True,
    float_tol_ndigits: int | None = 6,
    sort_columns: bool = False,
) -> bool:
    """
    比较两个查询结果是否"正确匹配"。

    Args:
        predicted_rows: 模型生成 SQL 的执行结果。
        ground_truth_rows: ground truth SQL 的执行结果。
        strict: True 时完全对齐 BIRD 官方口径（set 直接比较，无容差）；
                False 时使用 float_tol_ndigits / sort_columns 做宽松比较，
                仅用于调试分析或人工检查，不建议用于训练 reward。
        float_tol_ndigits: 宽松模式下浮点数保留的小数位数。
        sort_columns: 宽松模式下是否对每行内部的列做排序（忽略列顺序）。

    Returns:
        bool，两个结果集是否视为匹配。
    """
    if strict:
        try:
            return set(predicted_rows) == set(ground_truth_rows)
        except TypeError:
            # 极少数情况下行内包含不可哈希类型（理论上 sqlite 不会返回，
            # 但防御性处理一下，退化为逐行字符串比较）。
            pred_str = sorted(str(r) for r in predicted_rows)
            gt_str = sorted(str(r) for r in ground_truth_rows)
            return pred_str == gt_str

    pred_set = _normalize_rows(predicted_rows, float_tol_ndigits, sort_columns)
    gt_set = _normalize_rows(ground_truth_rows, float_tol_ndigits, sort_columns)
    return pred_set == gt_set