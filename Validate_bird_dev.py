"""
validate_bird_dev.py
--------------------
批量执行 BIRD dev 集的 ground truth SQL，验证本地数据库和执行器是否可用。

这一步不评估模型，只检查数据和执行环境。如果 ground truth 自己都执行失败，
后续 baseline / GRPO 的 reward 信号就不可信。
"""

from __future__ import annotations

import json
import argparse
from collections import Counter
from pathlib import Path

from Executor import execute_sql

BIRD_DEV_ROOT = Path("datasets/bird/dev/dev_20240627")
DEV_JSON = BIRD_DEV_ROOT / "dev.json"
DEV_DB_ROOT = BIRD_DEV_ROOT / "dev_databases"


def get_db_path(db_id: str) -> Path:
    return DEV_DB_ROOT / db_id / f"{db_id}.sqlite"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    examples = json.loads(DEV_JSON.read_text(encoding="utf-8"))

    total = len(examples)
    success = 0
    missing_db = 0
    failures = []
    error_counter: Counter[str] = Counter()

    for idx, example in enumerate(examples, 1):
        db_id = example["db_id"]
        sql = example["SQL"]
        db_path = get_db_path(db_id)

        if not db_path.exists():
            missing_db += 1
            failures.append(
                {
                    "question_id": example.get("question_id"),
                    "db_id": db_id,
                    "error_type": "missing_db",
                    "error": f"database not found: {db_path}",
                    "sql": sql,
                }
            )
            continue

        result = execute_sql(str(db_path), sql, timeout_sec=args.timeout_sec)
        if result.success:
            success += 1
        else:
            error_type = result.error_type or "unknown"
            error_counter[error_type] += 1
            failures.append(
                {
                    "question_id": example.get("question_id"),
                    "db_id": db_id,
                    "error_type": error_type,
                    "error": result.error,
                    "sql": sql,
                }
            )

        if idx % 100 == 0:
            print(f"processed {idx}/{total}...")

    print("=" * 100)
    print(f"total: {total}")
    print(f"success: {success}")
    print(f"failed: {total - success}")
    print(f"missing_db: {missing_db}")
    print(f"timeout_sec: {args.timeout_sec}")
    print(f"success_rate: {success / total:.4f}")
    print(f"error_counter: {dict(error_counter)}")

    if failures:
        print("-" * 100)
        print("first 10 failures:")
        for item in failures[:10]:
            print(
                f"question_id={item['question_id']} db_id={item['db_id']} "
                f"error_type={item['error_type']} error={item['error']}"
            )
            print(f"sql={item['sql']}")


if __name__ == "__main__":
    main()
