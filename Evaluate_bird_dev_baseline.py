"""
evaluate_bird_dev_baseline.py
-----------------------------
评估原始 Qwen2.5-Coder-7B-Instruct 在 BIRD dev 上的单轮 Text-to-SQL 准确率。

这不是 LangGraph 多轮 Agent 评测，而是 GRPO 训练前的 raw policy baseline:
    question + schema + evidence -> SQL -> safety -> execute -> compare
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from Bird_schema_context import build_schema_context
from Compare import compare_results
from Executor import execute_sql
from Llm_client import VLLMClient
from Safety import check_sql_safety

BIRD_DEV_ROOT = Path("datasets/bird/dev/dev_20240627")
DEV_JSON = BIRD_DEV_ROOT / "dev.json"
DEV_TABLES_JSON = BIRD_DEV_ROOT / "dev_tables.json"
DEV_DB_ROOT = BIRD_DEV_ROOT / "dev_databases"
DEFAULT_OUTPUT = Path("outputs/bird_dev_baseline.jsonl")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--schema-mode", choices=["basic", "enhanced"], default="basic")
    return parser.parse_args()


def get_db_path(db_id: str) -> Path:
    return DEV_DB_ROOT / db_id / f"{db_id}.sqlite"


def load_table_map() -> dict[str, dict[str, Any]]:
    tables = json.loads(DEV_TABLES_JSON.read_text(encoding="utf-8"))
    return {item["db_id"]: item for item in tables}


def iter_examples(examples: list[dict[str, Any]], start: int, limit: int | None):
    end = None if limit is None else start + limit
    yield from examples[start:end]


def load_done_question_ids(output: Path) -> set[int]:
    if not output.exists():
        return set()
    done = set()
    with output.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            done.add(item["question_id"])
    return done


def main() -> None:
    args = parse_args()
    examples = json.loads(DEV_JSON.read_text(encoding="utf-8"))
    table_map = load_table_map()
    client = VLLMClient(model=args.model, base_url=args.base_url)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    done_ids = load_done_question_ids(args.output) if args.resume else set()
    mode = "a" if args.resume else "w"

    stats: Counter[str] = Counter()
    error_counter: Counter[str] = Counter()
    started = time.monotonic()

    with args.output.open(mode, encoding="utf-8") as out:
        for idx, example in enumerate(iter_examples(examples, args.start, args.limit), 1):
            question_id = example["question_id"]
            if question_id in done_ids:
                stats["resumed_skip"] += 1
                continue

            db_id = example["db_id"]
            db_path = get_db_path(db_id)
            schema_context = build_schema_context(
                table_map[db_id],
                db_root=DEV_DB_ROOT,
                db_id=db_id,
                mode=args.schema_mode,
            )
            question = example["question"]
            evidence = example.get("evidence", "")
            if evidence:
                question = f"{question}\nEvidence: {evidence}"

            record: dict[str, Any] = {
                "question_id": question_id,
                "db_id": db_id,
                "difficulty": example.get("difficulty"),
                "question": example["question"],
                "ground_truth_sql": example["SQL"],
                "schema_mode": args.schema_mode,
            }

            gt_result = execute_sql(str(db_path), example["SQL"], timeout_sec=args.timeout_sec)
            if not gt_result.success:
                stats["gt_failed"] += 1
                record.update(
                    {
                        "status": "gt_failed",
                        "gt_error_type": gt_result.error_type,
                        "gt_error": gt_result.error,
                    }
                )
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()
                continue

            try:
                predicted_sql = client.generate_sql(
                    question=question,
                    schema_context=schema_context,
                    error_history=[],
                )
            except Exception as exc:  # noqa: BLE001
                stats["llm_failed"] += 1
                record.update({"status": "llm_failed", "error": str(exc)})
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()
                continue

            record["predicted_sql"] = predicted_sql
            safety = check_sql_safety(predicted_sql)
            if not safety.is_safe:
                stats["safety_violation"] += 1
                record.update(
                    {
                        "status": "safety_violation",
                        "safety_reason": safety.reason,
                        "is_correct": False,
                    }
                )
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()
                continue

            pred_result = execute_sql(str(db_path), predicted_sql, timeout_sec=args.timeout_sec)
            if not pred_result.success:
                stats["pred_exec_failed"] += 1
                error_type = pred_result.error_type or "unknown"
                error_counter[error_type] += 1
                record.update(
                    {
                        "status": "pred_exec_failed",
                        "pred_error_type": error_type,
                        "pred_error": pred_result.error,
                        "is_correct": False,
                    }
                )
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()
                continue

            is_correct = compare_results(pred_result.rows, gt_result.rows, strict=True)
            stats["evaluated"] += 1
            if is_correct:
                stats["correct"] += 1

            record.update(
                {
                    "status": "ok",
                    "is_correct": is_correct,
                    "pred_columns": pred_result.columns,
                    "pred_row_count": len(pred_result.rows),
                    "gt_row_count": len(gt_result.rows),
                    "pred_elapsed_sec": pred_result.elapsed_sec,
                    "gt_elapsed_sec": gt_result.elapsed_sec,
                }
            )
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()

            attempted = sum(
                stats[k]
                for k in [
                    "gt_failed",
                    "llm_failed",
                    "safety_violation",
                    "pred_exec_failed",
                    "evaluated",
                ]
            )
            if attempted % 10 == 0:
                denominator = stats["evaluated"] + stats["pred_exec_failed"] + stats["safety_violation"]
                accuracy = stats["correct"] / denominator if denominator else 0.0
                print(
                    f"processed={attempted} correct={stats['correct']} "
                    f"accuracy={accuracy:.4f} elapsed={time.monotonic() - started:.1f}s"
                )

    denominator = stats["evaluated"] + stats["pred_exec_failed"] + stats["safety_violation"]
    accuracy = stats["correct"] / denominator if denominator else 0.0
    print("=" * 100)
    print(f"output: {args.output}")
    print(f"stats: {dict(stats)}")
    print(f"pred_error_counter: {dict(error_counter)}")
    print(f"accuracy: {accuracy:.4f}")
    print(f"elapsed_sec: {time.monotonic() - started:.1f}")


if __name__ == "__main__":
    main()
