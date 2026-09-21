#!/usr/bin/env bash
set -euo pipefail

cd /data1/tiantian/VeriSQL-Agent

/data1/tiantian/miniconda3/envs/verl/bin/python Prepare_bird_grpo_data.py \
  --validate-ground-truth \
  --train-size -1 \
  --val-size 200 \
  --schema-mode enhanced \
  --output-dir outputs/grpo_enhanced_full \
  --model-path models/Qwen2.5-Coder-7B-Instruct-BIRD-SFT-LoRA-Merged \
  --max-prompt-tokens 4096
