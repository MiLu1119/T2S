#!/usr/bin/env bash
set -euo pipefail

curl -sS 'http://127.0.0.1:8000/v1/chat/completions' \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON' | python -m json.tool
{"model":"qwen2.5-coder-7b","messages":[{"role":"system","content":"你是Text-to-SQL助手。只输出一条SQLite SELECT语句，不要解释。"},{"role":"user","content":"表 employees(emp_id, name, dept_id, salary, hire_date)。查询所有员工的户籍。"}],"temperature":0,"max_tokens":128}
JSON
