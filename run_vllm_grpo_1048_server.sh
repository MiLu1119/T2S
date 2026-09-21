#!/usr/bin/env bash
set -euo pipefail

export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-2}"
export VLLM_USE_FLASHINFER_SAMPLER=0

exec /data1/tiantian/miniconda3/envs/verl/bin/python /data1/tiantian/VeriSQL-Agent/run_vllm_grpo_1048_server.py
