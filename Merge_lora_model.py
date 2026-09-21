"""
merge_lora_model.py
-------------------
把 LoRA adapter 合并回 base model，并保存成一个独立的完整模型副本。

不会覆盖:
  - 原始 base model
  - LoRA adapter 目录
"""

from __future__ import annotations

import argparse
from pathlib import Path

import accelerate.utils.other as accelerate_other
import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

DEFAULT_BASE_MODEL = Path("models/Qwen2.5-Coder-7B-Instruct")
DEFAULT_LORA_PATH = Path("outputs/sft/qwen2_5_coder_7b_bird_lora")
DEFAULT_OUTPUT_DIR = Path("models/Qwen2.5-Coder-7B-Instruct-BIRD-SFT-LoRA-Merged")

# merge/save 不使用 DeepSpeed。verl 环境里装了 deepspeed 但没有 CUDA_HOME，
# accelerate 保存模型时误导入会触发 CUDA_HOME 检查失败。
accelerate_other.is_deepspeed_available = lambda: False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-model", type=Path, default=DEFAULT_BASE_MODEL)
    parser.add_argument("--lora-path", type=Path, default=DEFAULT_LORA_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise FileExistsError(
            f"output dir already exists and is not empty: {args.output_dir}. "
            "Choose another --output-dir to avoid overwriting a model."
        )

    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
    )
    model = PeftModel.from_pretrained(model, args.lora_path)
    merged_model = model.merge_and_unload()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    merged_model.save_pretrained(
        args.output_dir,
        safe_serialization=True,
        max_shard_size="4GB",
    )
    tokenizer.save_pretrained(args.output_dir)

    print(f"merged model saved to: {args.output_dir}")


if __name__ == "__main__":
    main()
