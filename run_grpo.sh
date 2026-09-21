#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/data1/tiantian/VeriSQL-Agent"
VERL_ROOT="/data1/tiantian/project1/verl"
PYTHON="/data1/tiantian/miniconda3/envs/verl/bin/python"

MODEL_PATH="${MODEL_PATH:-${PROJECT_ROOT}/models/Qwen2.5-Coder-7B-Instruct-BIRD-SFT-LoRA-Merged}"
TRAIN_FILE="${TRAIN_FILE:-${PROJECT_ROOT}/outputs/grpo/bird_train_grpo.parquet}"
VAL_FILE="${VAL_FILE:-${PROJECT_ROOT}/outputs/grpo/bird_val_grpo.parquet}"
REWARD_FILE="${REWARD_FILE:-${PROJECT_ROOT}/Bird_grpo_reward.py}"

export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0,1,2,3,4,5,6,7}"
export PYTHONPATH="${PROJECT_ROOT}:${VERL_ROOT}:${PYTHONPATH:-}"
export TOKENIZERS_PARALLELISM=false
export VLLM_USE_FLASHINFER_SAMPLER=0
export RAY_TMPDIR="${RAY_TMPDIR:-/tmp/ray_verisql_grpo}"
export HF_HOME="${HF_HOME:-${PROJECT_ROOT}/outputs/cache/hf_home}"
export HF_DATASETS_CACHE="${HF_DATASETS_CACHE:-${PROJECT_ROOT}/outputs/cache/hf_datasets}"
export TRANSFORMERS_CACHE="${TRANSFORMERS_CACHE:-${PROJECT_ROOT}/outputs/cache/transformers}"
export TRITON_CACHE_DIR="${TRITON_CACHE_DIR:-/tmp/triton_cache_verisql_grpo}"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_DATASETS_OFFLINE=1

NGPUS_PER_NODE="${NGPUS_PER_NODE:-8}"
TRAIN_BATCH_SIZE="${TRAIN_BATCH_SIZE:-8}"
PPO_MINI_BATCH_SIZE="${PPO_MINI_BATCH_SIZE:-16}"
ROLLOUT_N="${ROLLOUT_N:-2}"
ROLLOUT_TEMPERATURE="${ROLLOUT_TEMPERATURE:-0.8}"
ROLLOUT_TOP_P="${ROLLOUT_TOP_P:-0.95}"
ROLLOUT_GPU_MEMORY_UTILIZATION="${ROLLOUT_GPU_MEMORY_UTILIZATION:-0.20}"
ROLLOUT_MAX_NUM_BATCHED_TOKENS="${ROLLOUT_MAX_NUM_BATCHED_TOKENS:-2048}"
ROLLOUT_MAX_NUM_SEQS="${ROLLOUT_MAX_NUM_SEQS:-8}"
MAX_PROMPT_LENGTH="${MAX_PROMPT_LENGTH:-2048}"
MAX_RESPONSE_LENGTH="${MAX_RESPONSE_LENGTH:-128}"
PPO_MAX_TOKEN_LEN_PER_GPU="${PPO_MAX_TOKEN_LEN_PER_GPU:-4096}"
ACTOR_PARAM_OFFLOAD="${ACTOR_PARAM_OFFLOAD:-True}"
ACTOR_OPTIMIZER_OFFLOAD="${ACTOR_OPTIMIZER_OFFLOAD:-True}"
REWARD_TIMEOUT_SEC="${REWARD_TIMEOUT_SEC:-5.0}"
REWARD_USE_PARTIAL="${REWARD_USE_PARTIAL:-False}"
REWARD_PARTIAL_BONUS_CAP="${REWARD_PARTIAL_BONUS_CAP:-0.3}"
REWARD_ROW_F1_WEIGHT="${REWARD_ROW_F1_WEIGHT:-0.3}"
REWARD_VALUE_F1_WEIGHT="${REWARD_VALUE_F1_WEIGHT:-0.2}"
REWARD_SCALAR_WEIGHT="${REWARD_SCALAR_WEIGHT:-0.1}"
TOTAL_EPOCHS="${TOTAL_EPOCHS:-1}"
SAVE_FREQ="${SAVE_FREQ:-20}"
TEST_FREQ="${TEST_FREQ:--1}"
VAL_BEFORE_TRAIN="${VAL_BEFORE_TRAIN:-False}"
EXPERIMENT_NAME="${EXPERIMENT_NAME:-qwen2_5_coder_7b_bird_grpo_$(date +%Y%m%d_%H%M)}"
CKPT_DIR="${CKPT_DIR:-${PROJECT_ROOT}/outputs/grpo/checkpoints/${EXPERIMENT_NAME}}"

if [[ ! -f "${TRAIN_FILE}" || ! -f "${VAL_FILE}" ]]; then
  echo "GRPO parquet not found. Prepare it first:"
  echo "  ${PYTHON} Prepare_bird_grpo_data.py --extract-databases --train-size 2000 --val-size 200"
  exit 1
fi

TOTAL_ROLLOUTS=$((TRAIN_BATCH_SIZE * ROLLOUT_N))
if (( PPO_MINI_BATCH_SIZE % NGPUS_PER_NODE != 0 )); then
  echo "PPO_MINI_BATCH_SIZE=${PPO_MINI_BATCH_SIZE} must be divisible by NGPUS_PER_NODE=${NGPUS_PER_NODE}."
  echo "For the default 8-GPU setup, use PPO_MINI_BATCH_SIZE=32 or 64."
  exit 1
fi

if (( PPO_MINI_BATCH_SIZE > TOTAL_ROLLOUTS )); then
  echo "PPO_MINI_BATCH_SIZE=${PPO_MINI_BATCH_SIZE} must be <= TRAIN_BATCH_SIZE * ROLLOUT_N=${TOTAL_ROLLOUTS}."
  exit 1
fi

mkdir -p "${HF_HOME}" "${HF_DATASETS_CACHE}" "${TRANSFORMERS_CACHE}" "${TRITON_CACHE_DIR}" "${RAY_TMPDIR}"

cd "${PROJECT_ROOT}"

exec "${PYTHON}" -m verl.trainer.main_ppo \
  algorithm.adv_estimator=grpo \
  algorithm.use_kl_in_reward=False \
  data.train_files="['${TRAIN_FILE}']" \
  data.val_files="['${VAL_FILE}']" \
  data.train_batch_size="${TRAIN_BATCH_SIZE}" \
  data.val_batch_size="${TRAIN_BATCH_SIZE}" \
  data.max_prompt_length="${MAX_PROMPT_LENGTH}" \
  data.max_response_length="${MAX_RESPONSE_LENGTH}" \
  data.return_raw_chat=True \
  data.filter_overlong_prompts=False \
  data.filter_overlong_prompts_workers=4 \
  data.truncation=error \
  data.shuffle=True \
  data.dataloader_num_workers=2 \
  data.trust_remote_code=True \
  actor_rollout_ref.model.path="${MODEL_PATH}" \
  +actor_rollout_ref.model.override_config.attn_implementation=sdpa \
  actor_rollout_ref.model.use_remove_padding=True \
  actor_rollout_ref.model.enable_gradient_checkpointing=True \
  actor_rollout_ref.actor.optim.lr=1e-6 \
  actor_rollout_ref.actor.ppo_mini_batch_size="${PPO_MINI_BATCH_SIZE}" \
  actor_rollout_ref.actor.use_dynamic_bsz=True \
  actor_rollout_ref.actor.ppo_max_token_len_per_gpu="${PPO_MAX_TOKEN_LEN_PER_GPU}" \
  actor_rollout_ref.actor.use_kl_loss=True \
  actor_rollout_ref.actor.kl_loss_coef=0.001 \
  actor_rollout_ref.actor.kl_loss_type=low_var_kl \
  actor_rollout_ref.actor.entropy_coeff=0 \
  actor_rollout_ref.actor.fsdp_config.param_offload="${ACTOR_PARAM_OFFLOAD}" \
  actor_rollout_ref.actor.fsdp_config.optimizer_offload="${ACTOR_OPTIMIZER_OFFLOAD}" \
  actor_rollout_ref.rollout.name=vllm \
  actor_rollout_ref.rollout.tensor_model_parallel_size=2 \
  actor_rollout_ref.rollout.gpu_memory_utilization="${ROLLOUT_GPU_MEMORY_UTILIZATION}" \
  actor_rollout_ref.rollout.enforce_eager=True \
  actor_rollout_ref.rollout.n="${ROLLOUT_N}" \
  actor_rollout_ref.rollout.temperature="${ROLLOUT_TEMPERATURE}" \
  actor_rollout_ref.rollout.top_p="${ROLLOUT_TOP_P}" \
  actor_rollout_ref.rollout.log_prob_use_dynamic_bsz=True \
  actor_rollout_ref.rollout.log_prob_max_token_len_per_gpu="${PPO_MAX_TOKEN_LEN_PER_GPU}" \
  actor_rollout_ref.rollout.max_num_batched_tokens="${ROLLOUT_MAX_NUM_BATCHED_TOKENS}" \
  actor_rollout_ref.rollout.max_num_seqs="${ROLLOUT_MAX_NUM_SEQS}" \
  actor_rollout_ref.rollout.enable_prefix_caching=False \
  actor_rollout_ref.rollout.enable_chunked_prefill=False \
  actor_rollout_ref.rollout.max_model_len=$((MAX_PROMPT_LENGTH + MAX_RESPONSE_LENGTH)) \
  actor_rollout_ref.ref.log_prob_use_dynamic_bsz=True \
  actor_rollout_ref.ref.log_prob_max_token_len_per_gpu="${PPO_MAX_TOKEN_LEN_PER_GPU}" \
  actor_rollout_ref.ref.fsdp_config.param_offload=True \
  reward.custom_reward_function.path="${REWARD_FILE}" \
  reward.custom_reward_function.name=compute_score \
  +reward.custom_reward_function.reward_kwargs.timeout_sec="${REWARD_TIMEOUT_SEC}" \
  +reward.custom_reward_function.reward_kwargs.use_partial_reward="${REWARD_USE_PARTIAL}" \
  +reward.custom_reward_function.reward_kwargs.partial_bonus_cap="${REWARD_PARTIAL_BONUS_CAP}" \
  +reward.custom_reward_function.reward_kwargs.row_f1_weight="${REWARD_ROW_F1_WEIGHT}" \
  +reward.custom_reward_function.reward_kwargs.value_f1_weight="${REWARD_VALUE_F1_WEIGHT}" \
  +reward.custom_reward_function.reward_kwargs.scalar_weight="${REWARD_SCALAR_WEIGHT}" \
  reward.reward_manager.name=naive \
  reward.num_workers=8 \
  trainer.logger='["console"]' \
  trainer.project_name=verisql_agent \
  trainer.experiment_name="${EXPERIMENT_NAME}" \
  trainer.n_gpus_per_node="${NGPUS_PER_NODE}" \
  trainer.nnodes=1 \
  trainer.default_local_dir="${CKPT_DIR}" \
  trainer.save_freq="${SAVE_FREQ}" \
  trainer.test_freq="${TEST_FREQ}" \
  trainer.total_epochs="${TOTAL_EPOCHS}" \
  trainer.val_before_train="${VAL_BEFORE_TRAIN}" \
  "$@"
