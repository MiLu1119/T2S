"""
bird_grpo_reward.py
-------------------
verl GRPO 训练用的 BIRD Text-to-SQL rule reward。

训练时 verl 会传入:
  - solution_str: 模型生成的 completion
  - ground_truth: BIRD 标准 SQL
  - extra_info: Prepare_bird_grpo_data.py 写入的 db_path/db_id 等元信息

这里复用项目已有的 Reward.compute_reward，保证训练 reward 与评测口径一致。
"""

from __future__ import annotations

import re
from typing import Any

from Reward import compute_reward


def extract_sql(text: str) -> str:
    text = (text or "").strip()

    fenced = re.search(r"```(?:sql)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    match = re.search(r"\b(WITH|SELECT)\b", text, re.IGNORECASE)
    if match:
        text = text[match.start() :].strip()

    if ";" in text:
        text = text.split(";", 1)[0].strip() + ";"

    return text


def compute_score(
    data_source: str,
    solution_str: str,
    ground_truth: str,
    extra_info: dict[str, Any] | None = None,
    timeout_sec: float = 5.0,
    use_partial_reward: bool = False,
    partial_bonus_cap: float = 0.3,
    row_f1_weight: float = 0.3,
    value_f1_weight: float = 0.2,
    scalar_weight: float = 0.1,
) -> dict[str, Any]:
    extra_info = extra_info or {}
    db_path = extra_info.get("db_path")
    predicted_sql = extract_sql(solution_str)

    if not db_path:
        return {
            "score": 0.0,
            "passed_safety": False,
            "executed_successfully": False,
            "is_correct": False,
            "error_type": "missing_db_path",
            "predicted_sql": predicted_sql,
        }

    reward = compute_reward(
        db_path=str(db_path),
        predicted_sql=predicted_sql,
        ground_truth_sql=ground_truth,
        timeout_sec=timeout_sec,
        use_partial_reward=use_partial_reward,
        partial_bonus_cap=partial_bonus_cap,
        row_f1_weight=row_f1_weight,
        value_f1_weight=value_f1_weight,
        scalar_weight=scalar_weight,
    )
    return {
        "score": reward.total_reward,
        "passed_safety": reward.passed_safety,
        "executed_successfully": reward.executed_successfully,
        "is_correct": reward.is_correct,
        "error_type": reward.error_type or "",
        "elapsed_sec": reward.elapsed_sec,
        "detail": reward.detail,
        "partial_similarity": reward.partial_similarity,
        "partial_bonus": reward.partial_bonus,
        "predicted_sql": predicted_sql,
        "db_id": extra_info.get("db_id", ""),
        "data_source": data_source,
    }
