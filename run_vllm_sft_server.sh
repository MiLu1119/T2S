#!/usr/bin/env bash
  set -euo pipefail

  BASE_MODEL="/data1/tiantian/VeriSQL-Agent/models/Qwen2.5-Coder-7B-Instruct"
  LORA_PATH="/data1/tiantian/VeriSQL-Agent/outputs/sft/qwen2_5_coder_7b_bird_lora"

  export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-1}"
  export VLLM_USE_FLASHINFER_SAMPLER=0

  exec /data1/tiantian/miniconda3/envs/verl/bin/python -m vllm.entrypoints.openai.api_server \
    --model "${BASE_MODEL}" \
    --served-model-name qwen2.5-coder-7b-sft \
    --enable-lora \
    --lora-modules bird_sft="${LORA_PATH}" \
    --host 127.0.0.1 \
    --port 8001 \
    --dtype bfloat16 \
    --max-model-len 4096 \
    --gpu-memory-utilization 0.80 \
    --enforce-eager