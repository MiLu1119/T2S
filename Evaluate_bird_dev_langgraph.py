"""
Evaluate BIRD dev through the LangGraph Text-to-SQL workflow.

This differs from Evaluate_bird_dev_baseline.py:
  baseline: question + schema -> one SQL -> execute -> compare
  langgraph: question + schema -> SQL -> safety -> execute -> reflect/retry -> compare final result
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
from Evaluate_bird_dev_baseline import DEV_DB_ROOT, DEV_JSON, load_table_map, get_db_path
from Executor import execute_sql
from Graph import build_graph
from Llm_client import VLLMClient

DEFAULT_OUTPUT = Path("outputs/bird_dev_langgraph.jsonl")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--schema-mode", choices=["basic", "enhanced"], default="enhanced")
    parser.add_argument("--max-retries", type=int, default=3)
    return parser.parse_args()


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
            done.add(json.loads(line)["question_id"])
    return done


def build_question(example: dict[str, Any]) -> str:
    question = example["question"]
    evidence = example.get("evidence", "")
    if evidence:
        question = f"{question}\nEvidence: {evidence}"
    return question


def collect_attempted_sql(final_state: dict[str, Any]) -> list[str]:
    sqls = [item["sql"] for item in final_state.get("error_history", [])]
    current_sql = final_state.get("current_sql")
    if current_sql and (not sqls or sqls[-1] != current_sql):
        sqls.append(current_sql)
    return sqls


def main() -> None:
    args = parse_args()
    examples = json.loads(DEV_JSON.read_text(encoding="utf-8"))
    table_map = load_table_map()
    client = VLLMClient(model=args.model, base_url=args.base_url)
    graph = build_graph(
        llm_client=client,
        max_retries=args.max_retries,
        execute_timeout_sec=args.timeout_sec,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    done_ids = load_done_question_ids(args.output) if args.resume else set()
    mode = "a" if args.resume else "w"

    stats: Counter[str] = Counter()
    final_error_counter: Counter[str] = Counter()
    started = time.monotonic()

    with args.output.open(mode, encoding="utf-8") as out:
        for example in iter_examples(examples, args.start, args.limit):
            question_id = example["question_id"]
            if question_id in done_ids:
                stats["resumed_skip"] += 1
                continue

            db_id = example["db_id"]
            db_path = get_db_path(db_id)
            record: dict[str, Any] = {
                "question_id": question_id,
                "db_id": db_id,
                "difficulty": example.get("difficulty"),
                "question": example["question"],
                "ground_truth_sql": example["SQL"],
                "schema_mode": args.schema_mode,
                "max_retries": args.max_retries,
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

            schema_context = build_schema_context(
                table_map[db_id],
                db_root=DEV_DB_ROOT,
                db_id=db_id,
                mode=args.schema_mode,
            )
            initial_state = {
                "question": build_question(example),
                "db_path": str(db_path),
                "schema_context": schema_context,
                "retry_count": 0,
                "max_retries": args.max_retries,
                "error_history": [],
            }

            try:
                final_state = graph.invoke(initial_state)
            except Exception as exc:  # noqa: BLE001
                stats["llm_or_graph_failed"] += 1
                record.update({"status": "llm_or_graph_failed", "error": str(exc)})
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()
                continue

            attempted_sql = collect_attempted_sql(final_state)
            record.update(
                {
                    "agent_status": final_state.get("status"),
                    "retry_count": final_state.get("retry_count", 0),
                    "attempted_sql": attempted_sql,
                    "predicted_sql": final_state.get("current_sql", ""),
                    "error_history": final_state.get("error_history", []),
                }
            )

            if final_state.get("status") != "success":
                stats["human_handoff"] += 1
                last_error = (final_state.get("error_history") or [{}])[-1]
                error_type = last_error.get("error_type", "unknown")
                final_error_counter[error_type] += 1
                record.update(
                    {
                        "status": "human_handoff",
                        "is_correct": False,
                        "final_error_type": error_type,
                        "final_error": last_error.get("error", ""),
                    }
                )
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()
            else:
                pred_rows = final_state.get("execution_rows", [])
                is_correct = compare_results(pred_rows, gt_result.rows, strict=True)
                stats["evaluated"] += 1
                if is_correct:
                    stats["correct"] += 1
                record.update(
                    {
                        "status": "ok",
                        "is_correct": is_correct,
                        "pred_columns": final_state.get("execution_columns", []),
                        "pred_row_count": len(pred_rows),
                        "gt_row_count": len(gt_result.rows),
                    }
                )
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()

            attempted = (
                stats["gt_failed"]
                + stats["llm_or_graph_failed"]
                + stats["human_handoff"]
                + stats["evaluated"]
            )
            if attempted % 10 == 0:
                denominator = stats["evaluated"] + stats["human_handoff"]
                accuracy = stats["correct"] / denominator if denominator else 0.0
                print(
                    f"processed={attempted} correct={stats['correct']} "
                    f"accuracy={accuracy:.4f} handoff={stats['human_handoff']} "
                    f"elapsed={time.monotonic() - started:.1f}s"
                )

    denominator = stats["evaluated"] + stats["human_handoff"]
    accuracy = stats["correct"] / denominator if denominator else 0.0
    print("=" * 100)
    print(f"output: {args.output}")
    print(f"stats: {dict(stats)}")
    print(f"final_error_counter: {dict(final_error_counter)}")
    print(f"accuracy: {accuracy:.4f}")
    print(f"elapsed_sec: {time.monotonic() - started:.1f}")


if __name__ == "__main__":
    main()
