"""
prepare_bird_grpo_data.py
-------------------------
把 BIRD train 集转换成 verl GRPO/PPO trainer 可直接读取的 parquet。

输出格式遵循 verl 的 RLHF dataset 约定:
  {
    "data_source": "bird_text2sql",
    "prompt": [{"role": "system", ...}, {"role": "user", ...}],
    "ability": "text2sql",
    "reward_model": {"style": "rule", "ground_truth": "..."},
    "extra_info": {"db_path": "...", "db_id": "...", ...}
  }
"""

from __future__ import annotations

import argparse
import json
import random
import zipfile
from pathlib import Path
from typing import Any

from datasets import Dataset
from transformers import AutoTokenizer

from Executor import execute_sql
from Prepare_bird_sft_data import SYSTEM_PROMPT, build_schema_context, build_user_prompt

TRAIN_ROOT = Path("datasets/bird/train/train")
TRAIN_JSON = TRAIN_ROOT / "train.json"
TRAIN_TABLES_JSON = TRAIN_ROOT / "train_tables.json"
TRAIN_DB_ROOT = TRAIN_ROOT / "train_databases"
TRAIN_DB_ZIP = TRAIN_ROOT / "train_databases.zip"

DEFAULT_OUTPUT_DIR = Path("outputs/grpo")
DEFAULT_MODEL_PATH = Path("models/Qwen2.5-Coder-7B-Instruct-BIRD-SFT-LoRA-Merged")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--train-size", type=int, default=2000)
    parser.add_argument("--val-size", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--extract-databases", action="store_true")
    parser.add_argument("--validate-ground-truth", action="store_true")
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--max-prompt-tokens", type=int, default=4096)
    parser.add_argument("--schema-mode", choices=["basic", "enhanced"], default="basic")
    return parser.parse_args()


def load_table_map() -> dict[str, dict[str, Any]]:
    tables = json.loads(TRAIN_TABLES_JSON.read_text(encoding="utf-8"))
    return {item["db_id"]: item for item in tables}


def get_db_path(db_id: str) -> Path:
    return TRAIN_DB_ROOT / db_id / f"{db_id}.sqlite"


def maybe_extract_databases() -> None:
    if TRAIN_DB_ROOT.exists() and any(TRAIN_DB_ROOT.iterdir()):
        return
    if not TRAIN_DB_ZIP.exists():
        raise FileNotFoundError(
            f"train databases not found: {TRAIN_DB_ROOT}; zip not found: {TRAIN_DB_ZIP}"
        )
    print(f"extracting {TRAIN_DB_ZIP} -> {TRAIN_ROOT}")
    with zipfile.ZipFile(TRAIN_DB_ZIP) as zf:
        zf.extractall(TRAIN_ROOT)


def validate_database_layout(examples: list[dict[str, Any]]) -> None:
    missing = []
    for example in examples:
        db_path = get_db_path(example["db_id"])
        if not db_path.exists():
            missing.append(str(db_path))
            if len(missing) >= 5:
                break
    if missing:
        joined = "\n  ".join(missing)
        raise FileNotFoundError(
            "BIRD train databases are not extracted. Run:\n"
            "  python Prepare_bird_grpo_data.py --extract-databases\n"
            f"missing examples:\n  {joined}"
        )


def to_verl_row(
    example: dict[str, Any],
    table_map: dict[str, dict[str, Any]],
    index: int,
    split: str,
    schema_mode: str,
) -> dict:
    db_id = example["db_id"]
    schema_context = build_schema_context(
        table_map[db_id],
        db_root=TRAIN_DB_ROOT,
        db_id=db_id,
        schema_mode=schema_mode,
    )
    user_prompt = build_user_prompt(example, schema_context)
    db_path = get_db_path(db_id).resolve()
    return {
        "data_source": "bird_text2sql",
        "prompt": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "ability": "text2sql",
        "reward_model": {
            "style": "rule",
            "ground_truth": example["SQL"].strip(),
        },
        "extra_info": {
            "split": split,
            "index": index,
            "db_id": db_id,
            "db_path": str(db_path),
            "question": example["question"],
            "evidence": example.get("evidence", ""),
            "schema_mode": schema_mode,
        },
    }


def filter_gt_executable(examples: list[dict[str, Any]], timeout_sec: float) -> list[dict[str, Any]]:
    kept = []
    skipped = 0
    for idx, example in enumerate(examples, 1):
        db_path = get_db_path(example["db_id"])
        result = execute_sql(str(db_path), example["SQL"], timeout_sec=timeout_sec)
        if result.success:
            kept.append(example)
        else:
            skipped += 1
        if idx % 500 == 0:
            print(f"validated={idx} kept={len(kept)} skipped={skipped}", flush=True)
    print(f"ground-truth validation done: kept={len(kept)} skipped={skipped}", flush=True)
    return kept


def write_parquet(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Dataset.from_list(rows).to_parquet(str(path))
    print(f"wrote {len(rows)} rows to {path}")


def filter_prompt_length(rows: list[dict[str, Any]], model_path: Path, max_prompt_tokens: int) -> list[dict[str, Any]]:
    if max_prompt_tokens <= 0:
        return rows

    tokenizer = AutoTokenizer.from_pretrained(
        model_path,
        trust_remote_code=True,
        local_files_only=True,
    )
    kept = []
    skipped = 0
    for row in rows:
        prompt_text = tokenizer.apply_chat_template(
            row["prompt"],
            tokenize=False,
            add_generation_prompt=True,
        )
        token_ids = tokenizer(prompt_text, add_special_tokens=False)["input_ids"]
        if len(token_ids) <= max_prompt_tokens:
            kept.append(row)
        else:
            skipped += 1
    print(f"prompt length filter: kept={len(kept)} skipped={skipped} max_prompt_tokens={max_prompt_tokens}")
    return kept


def main() -> None:
    args = parse_args()
    if args.extract_databases:
        maybe_extract_databases()

    examples = json.loads(TRAIN_JSON.read_text(encoding="utf-8"))
    validate_database_layout(examples)

    rng = random.Random(args.seed)
    shuffled = list(examples)
    rng.shuffle(shuffled)

    train_size = len(shuffled) if args.train_size < 0 else args.train_size
    requested = train_size + args.val_size
    selected = shuffled[: min(requested, len(shuffled))]

    if args.validate_ground_truth:
        selected = filter_gt_executable(selected, timeout_sec=args.timeout_sec)

    train_examples = selected[:train_size]
    val_examples = selected[train_size : train_size + args.val_size]
    if args.val_size > 0 and not val_examples:
        val_examples = train_examples[: min(args.val_size, len(train_examples))]
        print(
            "WARNING: validation split is sampled from train examples because "
            "the requested train split consumed the full dataset. "
            "Use it only for verl dataloader/quick reward checks, not final evaluation."
        )

    table_map = load_table_map()
    train_rows = [
        to_verl_row(row, table_map, idx, "train", args.schema_mode)
        for idx, row in enumerate(train_examples)
    ]
    val_rows = [
        to_verl_row(row, table_map, idx, "val", args.schema_mode)
        for idx, row in enumerate(val_examples)
    ]
    train_rows = filter_prompt_length(train_rows, args.model_path, args.max_prompt_tokens)
    val_rows = filter_prompt_length(val_rows, args.model_path, args.max_prompt_tokens)

    write_parquet(train_rows, args.output_dir / "bird_train_grpo.parquet")
    write_parquet(val_rows, args.output_dir / "bird_val_grpo.parquet")


if __name__ == "__main__":
    main()
