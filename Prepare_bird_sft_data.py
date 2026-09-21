"""
prepare_bird_sft_data.py
------------------------
从 BIRD train 集抽样生成 LoRA SFT 用的 chat JSONL。

输出格式:
  {"messages": [{"role": "system", ...}, {"role": "user", ...}, {"role": "assistant", ...}], ...}
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

from Bird_schema_context import build_schema_context as build_bird_schema_context

TRAIN_ROOT = Path("datasets/bird/train/train")
TRAIN_JSON = TRAIN_ROOT / "train.json"
TRAIN_TABLES_JSON = TRAIN_ROOT / "train_tables.json"
TRAIN_DB_ROOT = TRAIN_ROOT / "train_databases"
DEFAULT_OUTPUT = Path("outputs/sft/bird_train_sft_1000.jsonl")

SYSTEM_PROMPT = (
    "你是企业级Text-to-SQL助手。"
    "只允许输出一条可执行的SQLite SELECT语句。"
    "不要输出解释、注释、Markdown代码块或多条SQL。"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--num-samples", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--schema-mode", choices=["basic", "enhanced"], default="basic")
    return parser.parse_args()


def load_table_map() -> dict[str, dict[str, Any]]:
    tables = json.loads(TRAIN_TABLES_JSON.read_text(encoding="utf-8"))
    return {item["db_id"]: item for item in tables}


def build_schema_context(
    table_info: dict[str, Any],
    db_root: Path | None = None,
    db_id: str | None = None,
    schema_mode: str = "basic",
) -> str:
    return build_bird_schema_context(
        table_info,
        db_root=db_root,
        db_id=db_id,
        mode=schema_mode,
    )


def build_user_prompt(example: dict[str, Any], schema_context: str) -> str:
    parts = [
        "请根据下面的数据库Schema和用户问题生成一条SQLite SQL。",
        "",
        "数据库Schema:",
        schema_context,
        "",
        "用户问题:",
        example["question"],
    ]
    evidence = example.get("evidence")
    if evidence:
        parts.extend(["", "Evidence:", evidence])
    parts.extend(
        [
            "",
            "要求:",
            "- 只输出一条SELECT语句。",
            "- 表名和列名必须使用Schema里的原始名称；包含空格、括号、百分号等特殊字符的列名必须用反引号包住。",
            "- 不要输出解释、前后缀说明或Markdown代码块。",
        ]
    )
    return "\n".join(parts)


def main() -> None:
    args = parse_args()
    examples = json.loads(TRAIN_JSON.read_text(encoding="utf-8"))
    table_map = load_table_map()

    rng = random.Random(args.seed)
    selected = rng.sample(examples, min(args.num_samples, len(examples)))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        for idx, example in enumerate(selected):
            db_id = example["db_id"]
            schema_context = build_schema_context(
                table_map[db_id],
                db_root=TRAIN_DB_ROOT,
                db_id=db_id,
                schema_mode=args.schema_mode,
            )
            item = {
                "id": idx,
                "db_id": db_id,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_user_prompt(example, schema_context)},
                    {"role": "assistant", "content": example["SQL"].strip()},
                ],
            }
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"wrote {len(selected)} examples to {args.output}")


if __name__ == "__main__":
    main()
