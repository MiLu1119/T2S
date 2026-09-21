#!/usr/bin/env bash
set -euo pipefail

exec /data1/tiantian/miniconda3/envs/verl/bin/python Evaluate_bird_dev_langgraph.py \
  --schema-mode enhanced \
  --model qwen2.5-coder-7b-grpo-first \
  --base-url http://127.0.0.1:8004/v1 \
  --timeout-sec 30 \
  --max-retries 3 \
  --output outputs/bird_dev_langgraph_grpo_first_full_v2.jsonl
