"""
sample_bird_reward_variance.py
------------------------------
在正式 GRPO 前做温度采样检查：
  - 对 BIRD dev 中若干问题，每题采样 K 条 candidate SQL
  - 用现有 safety / executor / compare 给每条 candidate 打 reward
  - 统计组内 reward 方差、唯一 SQL 数、全组同分比例

这个脚本回答的问题不是“准确率是多少”，而是：
SFT 后模型是否过度收敛，导致 GRPO 每组样本的 reward 没有区分度。
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

from Analyze_partial_reward import partial_similarity
from Compare import compare_results
from Evaluate_bird_dev_baseline import (
    DEV_JSON,
    DEV_DB_ROOT,
    build_schema_context,
    get_db_path,
    load_table_map,
)
from Executor import execute_sql
from Llm_client import VLLMClient
from Safety import check_sql_safety

SAFETY_PENALTY = -1.0
EXECUTION_REWARD = 0.3
CORRECT_REWARD = 1.3
CONSERVATIVE_PARTIAL_BONUS_WEIGHT = 0.3
DEFAULT_OUTPUT = Path("outputs/bird_dev_reward_variance.jsonl")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--num-candidates", type=int, default=8)
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001/v1")
    parser.add_argument("--model", default="qwen2.5-coder-7b-sft-merged")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--schema-mode", choices=["basic", "enhanced"], default="basic")
    parser.add_argument("--reward-mode", choices=["strict", "partial"], default="strict")
    return parser.parse_args()


def build_question(example: dict[str, Any]) -> str:
    question = example["question"]
    evidence = example.get("evidence", "")
    if evidence:
        question = f"{question}\nEvidence: {evidence}"
    return question


def sample_candidates(
    client: VLLMClient,
    question: str,
    schema_context: str,
    num_candidates: int,
    temperature: float,
    top_p: float,
    max_tokens: int,
) -> list[str]:
    client._ensure_server_ready()
    messages = [
        {
            "role": "system",
            "content": (
                "你是企业级Text-to-SQL助手。"
                "只允许输出一条可执行的SQLite SELECT语句。"
                "不要输出解释、注释、Markdown代码块或多条SQL。"
            ),
        },
        {
            "role": "user",
            "content": client._build_prompt(question, schema_context, error_history=[]),
        },
    ]
    response = client.client.chat.completions.create(
        model=client.model,
        messages=messages,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        n=num_candidates,
    )
    return [client._extract_sql(choice.message.content or "") for choice in response.choices]


def score_candidate(db_path: Path, sql: str, gt_rows: list[tuple], timeout_sec: float) -> dict[str, Any]:
    safety = check_sql_safety(sql)
    if not safety.is_safe:
        return {
            "sql": sql,
            "status": "safety_violation",
            "reward": SAFETY_PENALTY,
            "strict_reward": SAFETY_PENALTY,
            "partial_reward": SAFETY_PENALTY,
            "is_correct": False,
            "error_type": "safety",
            "error": safety.reason,
        }

    result = execute_sql(str(db_path), sql, timeout_sec=timeout_sec)
    if not result.success:
        return {
            "sql": sql,
            "status": "pred_exec_failed",
            "reward": 0.0,
            "strict_reward": 0.0,
            "partial_reward": 0.0,
            "is_correct": False,
            "error_type": result.error_type,
            "error": result.error,
        }

    is_correct = compare_results(result.rows, gt_rows, strict=True)
    strict_reward = CORRECT_REWARD if is_correct else EXECUTION_REWARD
    partial_scores = partial_similarity(result.rows, gt_rows)
    conservative_partial = conservative_partial_reward(
        is_correct=is_correct,
        partial_scores=partial_scores,
    )
    return {
        "sql": sql,
        "status": "ok",
        "reward": strict_reward,
        "strict_reward": strict_reward,
        "partial_reward": conservative_partial,
        "partial_scores": partial_scores,
        "is_correct": is_correct,
        "row_count": len(result.rows),
        "elapsed_sec": result.elapsed_sec,
    }


def conservative_partial_reward(is_correct: bool, partial_scores: dict[str, float]) -> float:
    if is_correct:
        return CORRECT_REWARD

    # Keep scalar closeness weak: many wrong SQLs produce close scalar values by
    # chance, so row/value overlap should dominate the training signal.
    partial_bonus = max(
        0.30 * partial_scores["row_f1"],
        0.20 * partial_scores["value_f1"],
        0.10 * partial_scores["scalar_similarity"],
    )
    partial_bonus = min(partial_bonus, CONSERVATIVE_PARTIAL_BONUS_WEIGHT)
    return EXECUTION_REWARD + partial_bonus


def main() -> None:
    args = parse_args()
    examples = json.loads(DEV_JSON.read_text(encoding="utf-8"))
    table_map = load_table_map()
    client = VLLMClient(
        model=args.model,
        base_url=args.base_url,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    stats: Counter[str] = Counter()
    variances = []
    strict_variances = []
    partial_variances = []
    unique_sql_counts = []
    started = time.monotonic()

    with args.output.open("w", encoding="utf-8") as out:
        for example in examples[args.start:]:
            if stats["groups_scored"] >= args.limit:
                break

            db_id = example["db_id"]
            db_path = get_db_path(db_id)
            gt_result = execute_sql(str(db_path), example["SQL"], timeout_sec=args.timeout_sec)
            if not gt_result.success:
                stats["gt_failed"] += 1
                continue

            schema_context = build_schema_context(
                table_map[db_id],
                db_root=DEV_DB_ROOT,
                db_id=db_id,
                mode=args.schema_mode,
            )
            question = build_question(example)
            candidates_sql = sample_candidates(
                client=client,
                question=question,
                schema_context=schema_context,
                num_candidates=args.num_candidates,
                temperature=args.temperature,
                top_p=args.top_p,
                max_tokens=args.max_tokens,
            )
            candidates = [
                score_candidate(db_path, sql, gt_result.rows, args.timeout_sec)
                for sql in candidates_sql
            ]
            if args.reward_mode == "partial":
                for item in candidates:
                    item["reward"] = item["partial_reward"]
            rewards = [item["reward"] for item in candidates]
            strict_rewards = [item["strict_reward"] for item in candidates]
            partial_rewards = [item["partial_reward"] for item in candidates]
            reward_variance = statistics.pvariance(rewards) if len(rewards) > 1 else 0.0
            strict_reward_variance = (
                statistics.pvariance(strict_rewards) if len(strict_rewards) > 1 else 0.0
            )
            partial_reward_variance = (
                statistics.pvariance(partial_rewards) if len(partial_rewards) > 1 else 0.0
            )
            unique_sql_count = len(set(candidates_sql))

            variances.append(reward_variance)
            strict_variances.append(strict_reward_variance)
            partial_variances.append(partial_reward_variance)
            unique_sql_counts.append(unique_sql_count)
            stats["groups_scored"] += 1
            stats["candidates_scored"] += len(candidates)
            if strict_reward_variance == 0:
                stats["zero_strict_reward_variance_groups"] += 1
            if partial_reward_variance == 0:
                stats["zero_partial_reward_variance_groups"] += 1
            if partial_reward_variance > strict_reward_variance:
                stats["partial_variance_improved_groups"] += 1
            elif partial_reward_variance < strict_reward_variance:
                stats["partial_variance_reduced_groups"] += 1
            else:
                stats["partial_variance_tied_groups"] += 1
            if unique_sql_count == 1:
                stats["single_unique_sql_groups"] += 1
            if any(item["is_correct"] for item in candidates):
                stats["groups_with_correct"] += 1

            for item in candidates:
                stats[f"candidate_status_{item['status']}"] += 1
                if item["is_correct"]:
                    stats["correct_candidates"] += 1

            record = {
                "question_id": example.get("question_id"),
                "db_id": db_id,
                "difficulty": example.get("difficulty"),
                "question": example["question"],
                "schema_mode": args.schema_mode,
                "reward_mode": args.reward_mode,
                "reward_variance": reward_variance,
                "strict_reward_variance": strict_reward_variance,
                "partial_reward_variance": partial_reward_variance,
                "unique_sql_count": unique_sql_count,
                "rewards": rewards,
                "strict_rewards": strict_rewards,
                "partial_rewards": partial_rewards,
                "candidates": candidates,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()

            if stats["groups_scored"] % 10 == 0:
                mean_var = sum(variances) / len(variances)
                mean_strict_var = sum(strict_variances) / len(strict_variances)
                mean_partial_var = sum(partial_variances) / len(partial_variances)
                mean_unique = sum(unique_sql_counts) / len(unique_sql_counts)
                print(
                    f"groups={stats['groups_scored']} "
                    f"mean_active_reward_variance={mean_var:.4f} "
                    f"mean_strict_variance={mean_strict_var:.4f} "
                    f"mean_partial_variance={mean_partial_var:.4f} "
                    f"partial_improved_rate={stats['partial_variance_improved_groups'] / stats['groups_scored']:.4f} "
                    f"mean_unique_sql={mean_unique:.2f} "
                    f"elapsed={time.monotonic() - started:.1f}s"
                )

    mean_var = sum(variances) / len(variances) if variances else 0.0
    mean_strict_var = sum(strict_variances) / len(strict_variances) if strict_variances else 0.0
    mean_partial_var = sum(partial_variances) / len(partial_variances) if partial_variances else 0.0
    mean_unique = sum(unique_sql_counts) / len(unique_sql_counts) if unique_sql_counts else 0.0
    zero_strict_var_rate = (
        stats["zero_strict_reward_variance_groups"] / stats["groups_scored"]
        if stats["groups_scored"]
        else 0.0
    )
    zero_partial_var_rate = (
        stats["zero_partial_reward_variance_groups"] / stats["groups_scored"]
        if stats["groups_scored"]
        else 0.0
    )
    partial_improved_rate = (
        stats["partial_variance_improved_groups"] / stats["groups_scored"]
        if stats["groups_scored"]
        else 0.0
    )
    single_sql_rate = (
        stats["single_unique_sql_groups"] / stats["groups_scored"]
        if stats["groups_scored"]
        else 0.0
    )
    candidate_accuracy = (
        stats["correct_candidates"] / stats["candidates_scored"]
        if stats["candidates_scored"]
        else 0.0
    )

    print("=" * 100)
    print(f"output: {args.output}")
    print(f"stats: {dict(stats)}")
    print(f"mean_active_reward_variance: {mean_var:.4f}")
    print(f"mean_strict_reward_variance: {mean_strict_var:.4f}")
    print(f"mean_partial_reward_variance: {mean_partial_var:.4f}")
    print(f"zero_strict_reward_variance_group_rate: {zero_strict_var_rate:.4f}")
    print(f"zero_partial_reward_variance_group_rate: {zero_partial_var_rate:.4f}")
    print(f"partial_variance_improved_group_rate: {partial_improved_rate:.4f}")
    print(f"mean_unique_sql_count: {mean_unique:.2f}")
    print(f"single_unique_sql_group_rate: {single_sql_rate:.4f}")
    print(f"candidate_accuracy: {candidate_accuracy:.4f}")
    print(f"elapsed_sec: {time.monotonic() - started:.1f}")


if __name__ == "__main__":
    main()
