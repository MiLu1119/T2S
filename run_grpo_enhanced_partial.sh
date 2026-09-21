#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/data1/tiantian/VeriSQL-Agent"

TRAIN_FILE="${PROJECT_ROOT}/outputs/grpo_enhanced_full/bird_train_grpo.parquet"
VAL_FILE="${PROJECT_ROOT}/outputs/grpo_enhanced_full/bird_val_grpo.parquet"
MODEL_PATH="${PROJECT_ROOT}/models/Qwen2.5-Coder-7B-Instruct-BIRD-SFT-LoRA-Merged"
EXPERIMENT_NAME="qwen2_5_coder_7b_bird_grpo_enhanced_partial"
CKPT_DIR="${PROJECT_ROOT}/outputs/grpo/checkpoints/${EXPERIMENT_NAME}"

if [[ ! -f "${TRAIN_FILE}" || ! -f "${VAL_FILE}" ]]; then
  echo "GRPO enhanced full parquet not found."
  echo
  echo "Prepare it first:"
  echo "  ./prepare_grpo_enhanced_full.sh"
  exit 1
fi

cd "${PROJECT_ROOT}"

CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 \
NGPUS_PER_NODE=8 \
TRAIN_FILE="${TRAIN_FILE}" \
VAL_FILE="${VAL_FILE}" \
MODEL_PATH="${MODEL_PATH}" \
REWARD_USE_PARTIAL=True \
EXPERIMENT_NAME="${EXPERIMENT_NAME}" \
CKPT_DIR="${CKPT_DIR}" \
TRAIN_BATCH_SIZE=8 \
PPO_MINI_BATCH_SIZE=16 \
ROLLOUT_N=2 \
ROLLOUT_GPU_MEMORY_UTILIZATION=0.30 \
ROLLOUT_MAX_NUM_BATCHED_TOKENS=8192 \
ROLLOUT_MAX_NUM_SEQS=8 \
MAX_PROMPT_LENGTH=4096 \
MAX_RESPONSE_LENGTH=128 \
TOTAL_EPOCHS=1 \
SAVE_FREQ=20 \
TEST_FREQ=-1 \
VAL_BEFORE_TRAIN=False \
bash run_grpo.sh
