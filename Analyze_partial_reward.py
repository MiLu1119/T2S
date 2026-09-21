"""
Analyze partial reward signals for execution-success-but-wrong SQL cases.

This script does not change the training reward. It re-executes predicted and
ground-truth SQL from an evaluation JSONL file, then estimates whether wrong
but executable candidates can be separated by a partial-match score.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

from Executor import execute_sql


DEFAULT_DEV_DB_ROOT = Path("datasets/bird/dev/dev_20240627/dev_databases")
EXECUTION_REWARD = 0.3
PARTIAL_BONUS_WEIGHT = 0.7


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


def normalize_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    return value


def normalize_row(row: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(normalize_value(value) for value in row)


def row_f1(pred_rows: list[tuple[Any, ...]], gt_rows: list[tuple[Any, ...]]) -> float:
    if not pred_rows or not gt_rows:
        return 0.0
    pred_set = {normalize_row(row) for row in pred_rows}
    gt_set = {normalize_row(row) for row in gt_rows}
    overlap = len(pred_set & gt_set)
    if overlap == 0:
        return 0.0
    precision = overlap / len(pred_set)
    recall = overlap / len(gt_set)
    return 2 * precision * recall / (precision + recall)


def value_f1(pred_rows: list[tuple[Any, ...]], gt_rows: list[tuple[Any, ...]]) -> float:
    pred_counter = Counter(flatten_values(pred_rows))
    gt_counter = Counter(flatten_values(gt_rows))
    if not pred_counter or not gt_counter:
        return 0.0
    overlap = sum((pred_counter & gt_counter).values())
    if overlap == 0:
        return 0.0
    precision = overlap / sum(pred_counter.values())
    recall = overlap / sum(gt_counter.values())
    return 2 * precision * recall / (precision + recall)


def flatten_values(rows: list[tuple[Any, ...]]) -> list[Any]:
    values = []
    for row in rows:
        values.extend(normalize_value(value) for value in row)
    return values


def scalar_similarity(pred_rows: list[tuple[Any, ...]], gt_rows: list[tuple[Any, ...]]) -> float:
    if len(pred_rows) != 1 or len(gt_rows) != 1:
        return 0.0
    if len(pred_rows[0]) != 1 or len(gt_rows[0]) != 1:
        return 0.0
    pred_value = pred_rows[0][0]
    gt_value = gt_rows[0][0]
    if not isinstance(pred_value, (int, float)) or not isinstance(gt_value, (int, float)):
        return 0.0
    if pred_value == gt_value:
        return 1.0
    denom = max(abs(float(gt_value)), 1.0)
    rel_error = abs(float(pred_value) - float(gt_value)) / denom
    if not math.isfinite(rel_error):
        return 0.0
    return max(0.0, 1.0 - min(rel_error, 1.0))


def row_count_similarity(pred_rows: list[tuple[Any, ...]], gt_rows: list[tuple[Any, ...]]) -> float:
    pred_count = len(pred_rows)
    gt_count = len(gt_rows)
    if pred_count == 0 or gt_count == 0:
        return 0.0
    return min(pred_count, gt_count) / max(pred_count, gt_count)


def partial_similarity(pred_rows: list[tuple[Any, ...]], gt_rows: list[tuple[Any, ...]]) -> dict[str, float]:
    scores = {
        "row_f1": row_f1(pred_rows, gt_rows),
        "value_f1": value_f1(pred_rows, gt_rows),
        "scalar_similarity": scalar_similarity(pred_rows, gt_rows),
        "row_count_similarity": row_count_similarity(pred_rows, gt_rows),
    }
    # Conservative combined score: exact row overlap is strongest; scalar
    # closeness helps aggregation cases; value overlap is useful but downweighted
    # because it can match coincidental values from wrong columns.
    scores["combined"] = max(
        scores["row_f1"],
        scores["scalar_similarity"],
        0.6 * scores["value_f1"],
    )
    scores["partial_reward"] = EXECUTION_REWARD + PARTIAL_BONUS_WEIGHT * scores["combined"]
    return scores


def load_wrong_ok_cases(path: Path) -> list[dict[str, Any]]:
    cases = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("status") == "ok" and item.get("is_correct") is False:
                cases.append(item)
    return cases


def bucket(score: float) -> str:
    if score == 0:
        return "0"
    if score <= 0.25:
        return "(0,0.25]"
    if score <= 0.50:
        return "(0.25,0.50]"
    if score <= 0.75:
        return "(0.50,0.75]"
    if score < 1.0:
        return "(0.75,1)"
    return "1"


def brief_case(item: dict[str, Any], scores: dict[str, float]) -> dict[str, Any]:
    return {
        "question_id": item["question_id"],
        "db_id": item["db_id"],
        "difficulty": item.get("difficulty"),
        "question": item["question"],
        "ground_truth_sql": item["ground_truth_sql"],
        "predicted_sql": item["predicted_sql"],
        "pred_row_count": item.get("pred_row_count"),
        "gt_row_count": item.get("gt_row_count"),
        "scores": {key: round(value, 4) for key, value in scores.items()},
    }


def main() -> None:
    args = parse_args()
    cases = load_wrong_ok_cases(args.input)

    distribution = Counter()
    reward_distribution = Counter()
    stats = Counter()
    scored_cases = []

    for item in cases:
        pred = execute_sql(
            str(db_path(args.db_root, item["db_id"])),
            item["predicted_sql"],
            timeout_sec=args.timeout_sec,
        )
        gt = execute_sql(
            str(db_path(args.db_root, item["db_id"])),
            item["ground_truth_sql"],
            timeout_sec=args.timeout_sec,
        )
        if not pred.success or not gt.success:
            stats["reexec_failed"] += 1
            continue

        scores = partial_similarity(pred.rows, gt.rows)
        scored_cases.append((item, scores))
        stats["scored"] += 1
        if scores["combined"] > 0:
            stats["nonzero_partial"] += 1
        if scores["combined"] >= 0.5:
            stats["combined_ge_0.5"] += 1
        if scores["combined"] >= 0.75:
            stats["combined_ge_0.75"] += 1
        distribution[bucket(scores["combined"])] += 1
        reward_distribution[bucket(scores["partial_reward"])] += 1

    combined_values = [scores["combined"] for _, scores in scored_cases]
    reward_values = [scores["partial_reward"] for _, scores in scored_cases]
    top_cases = sorted(scored_cases, key=lambda item: item[1]["combined"], reverse=True)[
        : args.sample_size
    ]

    report = {
        "input": str(args.input),
        "wrong_ok_total": len(cases),
        "stats": dict(stats),
        "combined_distribution": dict(distribution),
        "partial_reward_distribution": dict(reward_distribution),
        "combined_avg": round(sum(combined_values) / len(combined_values), 4)
        if combined_values
        else 0.0,
        "partial_reward_avg": round(sum(reward_values) / len(reward_values), 4)
        if reward_values
        else 0.0,
        "top_partial_cases": [brief_case(item, scores) for item, scores in top_cases],
    }

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.output}")
    print(text)


if __name__ == "__main__":
    main()
