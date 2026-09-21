"""
train_bird_sft_lora.py
----------------------
用 BIRD 抽样数据对 Qwen2.5-Coder-7B-Instruct 做轻量 LoRA SFT。

目标不是最终训练效果，而是得到一个格式更稳定、基础 SQL 能力略有提升的
SFT checkpoint，作为后续 GRPO 前的 warm start。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
import accelerate.utils.other as accelerate_other
from datasets import Dataset
from peft import LoraConfig, get_peft_model
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
)

DEFAULT_MODEL_PATH = Path("models/Qwen2.5-Coder-7B-Instruct")
DEFAULT_TRAIN_FILE = Path("outputs/sft/bird_train_sft_1000.jsonl")
DEFAULT_OUTPUT_DIR = Path("outputs/sft/qwen2_5_coder_7b_bird_lora")

# 这个轻量 LoRA SFT 不使用 DeepSpeed。verl 环境里虽然装了 deepspeed，
# 但没有 CUDA_HOME/nvcc，accelerate 误导入 deepspeed 会在 Trainer 初始化时报错。
accelerate_other.is_deepspeed_available = lambda: False


class DataCollatorForSFT:
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer

    def __call__(self, features: list[dict]) -> dict[str, torch.Tensor]:
        max_len = max(len(item["input_ids"]) for item in features)
        input_ids = []
        attention_mask = []
        labels = []

        for item in features:
            pad_len = max_len - len(item["input_ids"])
            input_ids.append(item["input_ids"] + [self.tokenizer.pad_token_id] * pad_len)
            attention_mask.append(item["attention_mask"] + [0] * pad_len)
            labels.append(item["labels"] + [-100] * pad_len)

        return {
            "input_ids": torch.tensor(input_ids, dtype=torch.long),
            "attention_mask": torch.tensor(attention_mask, dtype=torch.long),
            "labels": torch.tensor(labels, dtype=torch.long),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--train-file", type=Path, default=DEFAULT_TRAIN_FILE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max-length", type=int, default=4096)
    parser.add_argument("--num-train-epochs", type=float, default=1.0)
    parser.add_argument("--per-device-train-batch-size", type=int, default=1)
    parser.add_argument("--gradient-accumulation-steps", type=int, default=8)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--logging-steps", type=int, default=10)
    parser.add_argument("--save-steps", type=int, default=200)
    parser.add_argument("--max-steps", type=int, default=-1)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def build_dataset(path: Path, tokenizer, max_length: int) -> Dataset:
    rows = load_jsonl(path)

    def tokenize(example: dict) -> dict:
        messages = example["messages"]
        assistant_text = messages[-1]["content"]
        prompt_messages = messages[:-1]

        prompt = tokenizer.apply_chat_template(
            prompt_messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        full_text = prompt + assistant_text + tokenizer.eos_token

        tokenized = tokenizer(
            full_text,
            max_length=max_length,
            truncation=True,
            padding=False,
        )
        prompt_ids = tokenizer(
            prompt,
            max_length=max_length,
            truncation=True,
            padding=False,
        )["input_ids"]

        labels = list(tokenized["input_ids"])
        prompt_len = min(len(prompt_ids), len(labels))
        labels[:prompt_len] = [-100] * prompt_len
        tokenized["labels"] = labels
        return tokenized

    raw_dataset = Dataset.from_list(rows)
    return raw_dataset.map(
        tokenize,
        remove_columns=raw_dataset.column_names,
    )


def main() -> None:
    args = parse_args()

    tokenizer = AutoTokenizer.from_pretrained(args.model_path, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        args.model_path,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
    )
    model.enable_input_require_grads()

    lora_config = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=[
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ],
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    train_dataset = build_dataset(args.train_file, tokenizer, args.max_length)

    training_args = TrainingArguments(
        output_dir=str(args.output_dir),
        num_train_epochs=args.num_train_epochs,
        per_device_train_batch_size=args.per_device_train_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate,
        max_steps=args.max_steps,
        bf16=True,
        logging_steps=args.logging_steps,
        save_steps=args.save_steps,
        save_total_limit=2,
        report_to="none",
        remove_unused_columns=False,
        gradient_checkpointing=True,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        data_collator=DataCollatorForSFT(tokenizer),
    )
    trainer.train()
    trainer.save_model(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)


if __name__ == "__main__":
    main()
