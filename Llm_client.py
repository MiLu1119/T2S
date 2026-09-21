"""
llm_client.py
-------------
LLM 调用的统一入口。现在先用 MockLLM 跑通 LangGraph 的状态机逻辑，
后面接真实模型（vllm 起的 Qwen2.5-Coder-7B，或者训练完的 GRPO policy）
时，只需要新写一个 XxxLLM 类实现同样的 generate_sql() 接口，
graph.py 里不需要改任何东西。

MockLLM 的行为是故意设计过的，不是简单返回固定答案：
  - 第一次生成时，故意在某些问题上"手滑"引用一个不存在的字段，
    模拟真实模型偶尔会犯的错误
  - 收到 error_history（也就是执行报错信息）后，能"看懂"报错内容，
    去掉错误字段，生成修正后的 SQL
这样才能真正验证 Reflection 循环是"根据报错内容做出反应"，
而不是不管三七二十一地瞎重试。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import os
import re
import math
import json

import requests
from openai import OpenAI

from State import ErrorRecord
from Schema_catalog import identifier_description


class BaseLLMClient(ABC):
    last_trace: dict = {}

    @abstractmethod
    def generate_sql(
        self,
        question: str,
        schema_context: str,
        error_history: list[ErrorRecord],
    ) -> str:
        """根据问题、schema、历史错误信息，生成一条 SQL。"""
        raise NotImplementedError

    def describe_schema(self, tables: list[dict]) -> dict:
        """Return semantic descriptions keyed by table name; safe fallback for mock/local tests."""
        return {
            table["name"]: {
                "description": identifier_description(table["name"]),
                "columns": {
                    column["name"]: identifier_description(column["name"])
                    for column in table.get("columns", [])
                },
            }
            for table in tables
        }


class MockLLMClient(BaseLLMClient):
    """
    假的 LLM，用一个小型问题库模拟"生成 SQL"的行为，
    专门用来验证 LangGraph 状态机的路由逻辑，不代表真实模型能力。
    """

    def generate_sql(
        self,
        question: str,
        schema_context: str,
        error_history: list[ErrorRecord],
    ) -> str:
        self.last_trace = {
            "etc_enabled": False,
            "logprobs_available": False,
            "etc_triggered": False,
            "entropy": [],
            "model_calls": 0,
            "token_usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }
        first_attempt = len(error_history) == 0

        # ---- 场景 1：能正确回答，但第一次故意手滑写错字段名 ----
        if "华东" in question or "sales" in question.lower() and "amount" in question.lower():
            if first_attempt:
                # 故意引用一个不存在的字段 emp_dept_id（正确应该是 dept_id），
                # 模拟模型对 schema 记错了字段名的常见错误
                return (
                    "SELECT SUM(s.amount) FROM sales s "
                    "JOIN employees e ON s.emp_id = e.emp_id "
                    "WHERE e.emp_dept_id = 1"
                )
            else:
                # 看到报错里提到 "no such column: emp_dept_id"，修正为正确字段名
                last_error = error_history[-1]["error"]
                if "emp_dept_id" in last_error:
                    return (
                        "SELECT SUM(s.amount) FROM sales s "
                        "JOIN employees e ON s.emp_id = e.emp_id "
                        "WHERE e.dept_id = 1"
                    )

        # ---- 场景 2：平均薪资查询，一次就能生成对 ----
        if "平均" in question and "薪资" in question:
            return "SELECT dept_id, AVG(salary) FROM employees GROUP BY dept_id"

        # ---- 场景 3：故意设计一个"怎么都修不对"的问题，用来触发人工兜底 ----
        if "不存在的指标" in question:
            # 不管重试几次都引用一个不存在的字段，模拟模型对某类问题
            # 完全没有能力回答（比如 schema 里根本没有这个业务概念）
            return "SELECT nonexistent_metric FROM employees"

        # ---- 场景 4：模拟"第一次生成了危险SQL"，验证 safety 分支也能触发重试 ----
        if "危险测试" in question:
            if first_attempt:
                return "DELETE FROM employees"
            else:
                # 看到上一轮是 safety violation，退回一个安全的查询
                return "SELECT name FROM employees"

        # ---- 默认兜底：简单查询全部员工姓名 ----
        return "SELECT name FROM employees"


class VLLMClient(BaseLLMClient):
    """
    调用本地 vLLM OpenAI-compatible API 的真实模型客户端。

    默认对接:
      - base_url: http://127.0.0.1:8000/v1
      - model: qwen2.5-coder-7b
    """

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str = "EMPTY",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        request_timeout_sec: float = 60.0,
        enable_etc: bool = False,
        etc_first_diff_threshold: float = 0.12,
        etc_second_diff_threshold: float = 0.08,
        etc_window: int = 3,
        check_server_ready: bool = True,
    ):
        self.base_url = base_url or os.getenv("VLLM_BASE_URL", "http://127.0.0.1:8000/v1")
        self.model = model or os.getenv("VLLM_MODEL_NAME", "qwen2.5-coder-7b")
        self.client = OpenAI(base_url=self.base_url, api_key=api_key, timeout=request_timeout_sec)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._server_ready_checked = False
        self.check_server_ready = check_server_ready
        self.enable_etc = enable_etc
        self.etc_first_diff_threshold = etc_first_diff_threshold
        self.etc_second_diff_threshold = etc_second_diff_threshold
        self.etc_window = max(2, etc_window)
        self.last_trace = {}

    def generate_sql(
        self,
        question: str,
        schema_context: str,
        error_history: list[ErrorRecord],
    ) -> str:
        self._ensure_server_ready()
        messages = [
            {
                "role": "system",
                "content": (
                    "你是企业级Text-to-SQL助手。"
                    f"只允许输出一条可执行的{getattr(self, 'dialect', 'sqlite')} SELECT语句。"
                    "不要输出解释、注释、Markdown代码块或多条SQL。"
                ),
            },
            {
                "role": "user",
                "content": self._build_prompt(question, schema_context, error_history),
            },
        ]

        request = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.enable_etc:
            request.update({"logprobs": True, "top_logprobs": 10})
        model_calls = 0
        try:
            model_calls += 1
            response = self.client.chat.completions.create(**request)
        except Exception:
            # Some OpenAI-compatible providers do not implement logprobs. ETC is
            # optional, so retry without it while keeping the main agent usable.
            if not self.enable_etc:
                raise
            request.pop("logprobs", None)
            request.pop("top_logprobs", None)
            model_calls += 1
            response = self.client.chat.completions.create(**request)
        raw = response.choices[0].message.content or ""
        # Reasoning-capable OpenAI-compatible models can spend the complete
        # output budget on hidden reasoning and return an empty final answer.
        # Repeating the same graph retry cannot recover from that condition,
        # so retry once with a larger completion budget.
        if not raw.strip():
            request["max_tokens"] = max(1024, int(request["max_tokens"]) * 4)
            model_calls += 1
            response = self.client.chat.completions.create(**request)
            raw = response.choices[0].message.content or ""
        self.last_trace = self._build_generation_trace(response)
        self.last_trace["model_calls"] = model_calls
        return self._extract_sql(raw)

    def describe_schema(self, tables: list[dict]) -> dict:
        """Generate descriptions once during catalog enrichment, without sending data values."""
        prompt = (
            "你是企业数据目录专家。根据表名、字段名、类型、主键和关系，推断简洁中文业务含义。"
            "不得虚构具体数值、组织名称或未提供的业务规则。只输出JSON对象，键必须是原表名；"
            "每个值包含description和columns，columns的键必须是原字段名。\nSchema:\n"
            + json.dumps(tables, ensure_ascii=False)
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "只输出合法JSON，不要Markdown。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=max(1200, self.max_tokens),
        )
        text = (response.choices[0].message.content or "").strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("schema description model did not return a JSON object")
        parsed = json.loads(text[start:end + 1])
        if not isinstance(parsed, dict):
            raise ValueError("schema description response must be an object")
        return parsed

    def _ensure_server_ready(self) -> None:
        if not self.check_server_ready:
            return
        if self._server_ready_checked:
            return
        try:
            resp = requests.get(f"{self.base_url}/models", timeout=3)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Cannot reach vLLM server at {self.base_url}. "
                "Start the api_server first, or set VLLM_BASE_URL to the correct address."
            ) from exc
        self._server_ready_checked = True

    def _build_generation_trace(self, response) -> dict:
        entropies: list[float] = []
        tokens: list[str] = []
        logprobs = getattr(response.choices[0], "logprobs", None)
        for item in (getattr(logprobs, "content", None) or []):
            candidates = getattr(item, "top_logprobs", None) or []
            probs = [math.exp(float(candidate.logprob)) for candidate in candidates]
            remainder = max(0.0, 1.0 - sum(probs))
            if remainder:
                probs.append(remainder)
            entropy = -sum(p * math.log(max(p, 1e-12)) for p in probs)
            entropies.append(entropy)
            tokens.append(str(getattr(item, "token", "")))

        trigger_index = self._detect_etc_trigger(entropies) if self.enable_etc else None
        usage = getattr(response, "usage", None)
        token_usage = {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
        }
        return {
            "etc_enabled": self.enable_etc,
            "logprobs_available": bool(entropies),
            "etc_triggered": trigger_index is not None,
            "trigger_index": trigger_index,
            "trigger_prefix": "".join(tokens[: trigger_index + 1]) if trigger_index is not None else "",
            "entropy": [round(value, 5) for value in entropies],
            "token_usage": token_usage,
        }

    def _detect_etc_trigger(self, entropies: list[float]) -> int | None:
        if len(entropies) < self.etc_window + 2:
            return None
        first = [entropies[i] - entropies[i - 1] for i in range(1, len(entropies))]
        second = [first[i] - first[i - 1] for i in range(1, len(first))]
        for idx in range(self.etc_window - 1, len(second)):
            recent_first = first[idx - self.etc_window + 2 : idx + 2]
            recent_second = second[idx - self.etc_window + 1 : idx + 1]
            rising = sum(recent_first) / len(recent_first) >= self.etc_first_diff_threshold
            accelerating = max(recent_second) >= self.etc_second_diff_threshold
            if rising and accelerating:
                return idx + 2
        return None

    def _build_prompt(
        self,
        question: str,
        schema_context: str,
        error_history: list[ErrorRecord],
    ) -> str:
        parts = [
            "请根据下面的数据库Schema和用户问题生成一条SQLite SQL。",
            "",
            "数据库Schema:",
            schema_context,
            "",
            "用户问题:",
            question,
            "",
            "要求:",
            "- 只输出一条SELECT语句。",
            "- 表名和列名必须使用Schema里的原始名称；包含空格、括号、百分号等特殊字符的列名必须用反引号包住。",
            "- 不要使用INSERT、UPDATE、DELETE、DROP、ALTER、CREATE、PRAGMA、ATTACH。",
            "- 不要输出解释、前后缀说明或Markdown代码块。",
            "- 如果需要重试，请优先修复上一轮报错暴露的问题。",
        ]

        if error_history:
            parts.extend(["", "历史错误:"])
            for idx, err in enumerate(error_history, 1):
                parts.extend(
                    [
                        f"第{idx}次SQL:",
                        err["sql"],
                        f"错误类型: {err['error_type']}",
                        f"错误信息: {err['error']}",
                        "",
                    ]
                )

        return "\n".join(parts).strip()

    def _extract_sql(self, text: str) -> str:
        text = text.strip()

        fenced = re.search(r"```(?:sql)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()

        match = re.search(r"\b(WITH|SELECT)\b", text, re.IGNORECASE)
        if match:
            text = text[match.start():].strip()

        if ";" in text:
            text = text.split(";", 1)[0].strip() + ";"

        return text


# The endpoint protocol used by vLLM is the same one used by most hosted model
# gateways. Keep the historical name as an alias and expose a clearer public one.
OpenAICompatibleClient = VLLMClient
