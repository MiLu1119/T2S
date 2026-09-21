"""
Analyze execution-success-but-wrong Text-to-SQL cases.

This diagnostic script is for post-evaluation analysis only. It checks whether
wrong cases are genuinely wrong or only fail because strict result comparison is
too rigid, such as float precision or column order differences.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from Compare import compare_results
from Executor import execute_sql


DEFAULT_DEV_DB_ROOT = Path("datasets/bird/dev/dev_20240627/dev_databases")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--db-root", type=Path, default=DEFAULT_DEV_DB_ROOT)
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


def db_path(db_root: Path, db_id: str) -> Path:
    return db_root / db_id / f"{db_id}.sqlite"


def load_wrong_ok_cases(path: Path) -> list[dict[str, Any]]:
    cases = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("status") == "ok" and item.get("is_correct") is False:
                cases.append(item)
    return cases


def spread_sample(items: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    if size <= 0 or not items:
        return []
    if len(items) <= size:
        return items
    return [items[round(i * (len(items) - 1) / (size - 1))] for i in range(size)]


def classify_tolerant_matches(
    cases: list[dict[str, Any]],
    db_root: Path,
    timeout_sec: float,
) -> tuple[Counter[str], list[dict[str, Any]]]:
    stats: Counter[str] = Counter()
    recoverable = []

    for item in cases:
        path = db_path(db_root, item["db_id"])
        pred = execute_sql(str(path), item["predicted_sql"], timeout_sec=timeout_sec)
        gt = execute_sql(str(path), item["ground_truth_sql"], timeout_sec=timeout_sec)
        if not pred.success or not gt.success:
            stats["reexec_failed"] += 1
            continue

        if compare_results(pred.rows, gt.rows, strict=True):
            stats["strict_now_true"] += 1
            recoverable.append({"reason": "strict_now_true", **brief_case(item)})
        elif compare_results(pred.rows, gt.rows, strict=False, float_tol_ndigits=6, sort_columns=False):
            stats["float_tolerance_fix"] += 1
            recoverable.append({"reason": "float_tolerance_fix", **brief_case(item)})
        elif compare_results(pred.rows, gt.rows, strict=False, float_tol_ndigits=6, sort_columns=True):
            stats["column_order_fix"] += 1
            recoverable.append({"reason": "column_order_fix", **brief_case(item)})
        else:
            stats["still_wrong"] += 1

    return stats, recoverable


def brief_case(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "question_id": item["question_id"],
        "db_id": item["db_id"],
        "difficulty": item.get("difficulty"),
        "question": item["question"],
        "ground_truth_sql": item["ground_truth_sql"],
        "predicted_sql": item["predicted_sql"],
        "pred_columns": item.get("pred_columns"),
        "pred_row_count": item.get("pred_row_count"),
        "gt_row_count": item.get("gt_row_count"),
    }


def main() -> None:
    args = parse_args()
    wrong_ok_cases = load_wrong_ok_cases(args.input)

    row_count_stats: Counter[str] = Counter()
    for item in wrong_ok_cases:
        if item.get("pred_row_count") == item.get("gt_row_count"):
            row_count_stats["same_row_count"] += 1
        else:
            row_count_stats["different_row_count"] += 1
        if item.get("pred_row_count") == 0:
            row_count_stats["pred_empty"] += 1
        if item.get("gt_row_count") == 0:
            row_count_stats["gt_empty"] += 1

    tolerant_stats, recoverable = classify_tolerant_matches(
        wrong_ok_cases,
        args.db_root,
        args.timeout_sec,
    )
    sampled_cases = [brief_case(item) for item in spread_sample(wrong_ok_cases, args.sample_size)]

    report = {
        "input": str(args.input),
        "wrong_ok_total": len(wrong_ok_cases),
        "row_count_stats": dict(row_count_stats),
        "tolerant_compare_stats": dict(tolerant_stats),
        "recoverable_examples": recoverable[:50],
        "sampled_wrong_cases": sampled_cases,
    }

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.output}")
    print(text)


if __name__ == "__main__":
    main()
