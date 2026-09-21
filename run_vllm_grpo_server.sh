#!/usr/bin/env bash
set -euo pipefail

MODEL_PATH="/data1/tiantian/VeriSQL-Agent/models/Qwen2.5-Coder-7B-Instruct-BIRD-GRPO"
MODEL_NAME="qwen2.5-coder-7b-grpo"
HOST="127.0.0.1"
PORT="8002"

export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-2}"
export VLLM_USE_FLASHINFER_SAMPLER=0

exec /data1/tiantian/miniconda3/envs/verl/bin/python -m vllm.entrypoints.openai.api_server \
  --model "${MODEL_PATH}" \
  --served-model-name "${MODEL_NAME}" \
  --host "${HOST}" \
  --port "${PORT}" \
  --dtype bfloat16 \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.80 \
  --enforce-eager
