"""
graph.py
--------
用 LangGraph 把 Text-to-SQL 的核心闭环搭成一个状态机：

    START
      │
      ▼
  get_schema_context
      │
      ▼
  generate_sql  <───────────────┐
      │                         │
      ▼                         │
  validate_sql                  │ (重试：带着错误信息回到 generate_sql)
      │                         │
      ├── 不安全 ──────┐        │
      │                ▼        │
      │            reflect ─────┘
      │                │
      ▼                │
  execute_sql          │
      │                │
      └───────────────>┤
                        │
              ┌─────────┴─────────┐
              │                   │
           成功                 超过最大重试次数
              │                   │
              ▼                   ▼
        generate_answer      human_handoff
              │                   │
              ▼                   ▼
             END                 END

节点职责:
  - get_schema_context: 拿到喂给 LLM 的 schema 描述（第一版是静态文本，
    后续可以换成向量检索）
  - generate_sql: 调用 LLM（现在是 MockLLMClient）生成 SQL
  - validate_sql: 复用 bird_reward_engine 的 safety.py 做安全检查
  - execute_sql: 复用 bird_reward_engine 的 executor.py 真实执行 SQL
  - reflect: 这是一个"路由节点"，本身不改变业务数据，只根据当前
    execution_success / safety_check_passed / retry_count 决定下一步
    走 generate_sql（重试）还是 generate_answer（成功）还是
    human_handoff（超限）
  - generate_answer: 把执行结果转成自然语言（现在是简单模板，
    后续可以也换成 LLM 生成）
  - human_handoff: 终止节点，标记需要人工介入
"""

from __future__ import annotations

import re

from langgraph.graph import StateGraph, START, END
from opentelemetry import trace

from Executor import execute_sql as run_sql
from Llm_client import BaseLLMClient, MockLLMClient
from State import AgentState, ErrorRecord
from Retrieval import SQLiteSchemaRetriever
from Sql_validator import validate_sql_ast
from Database import AgentDatabase, SQLiteDatabase
from Telemetry import set_span_attributes
from Cancellation import cancellations


tracer = trace.get_tracer("verisql.graph")

# ---------------------------------------------------------------------------
# 静态 Schema 描述（对应 demo_db.py 里的三张表）。
# 后续升级为"Schema Retrieval"时，这里替换成向量检索出的相关表描述即可，
# 不需要改图结构。
# ---------------------------------------------------------------------------
DEMO_SCHEMA_CONTEXT = """
表 departments(部门表):
  - dept_id INTEGER, 主键
  - dept_name TEXT, 部门名称

表 employees(员工表):
  - emp_id INTEGER, 主键
  - name TEXT, 员工姓名
  - dept_id INTEGER, 外键关联 departments.dept_id
  - salary REAL, 薪资
  - hire_date TEXT, 入职日期

表 sales(销售记录表):
  - sale_id INTEGER, 主键
  - emp_id INTEGER, 外键关联 employees.emp_id
  - product TEXT, 产品名称
  - amount REAL, 销售金额
  - sale_date TEXT, 销售日期
""".strip()


def build_graph(
    llm_client: BaseLLMClient | None = None,
    max_retries: int = 3,
    execute_timeout_sec: float = 5.0,
    schema_retriever: SQLiteSchemaRetriever | None = None,
    database: AgentDatabase | None = None,
    max_result_rows: int = 1000,
    initial_schema_rag: bool = False,
    cost_enforce: bool = False,
    cost_max_estimated_cost: float = 100000.0,
    cost_max_estimated_rows: int = 1000000,
    cost_max_full_scans: int = 3,
    checkpointer=None,
):
    llm = llm_client or MockLLMClient()

    def is_cancelled(state: AgentState) -> bool:
        event = cancellations.get_event(state.get("request_id", ""))
        return bool(event is not None and event.is_set())

    def cancellation_event(state: AgentState):
        return cancellations.get_event(state.get("request_id", ""))

    def contextual_question(state: AgentState) -> str:
        history = state.get("conversation_history", [])
        if not history:
            return state["question"]
        lines = ["以下是同一数据库会话的历史查询，仅用于理解当前追问："]
        for item in history:
            lines.append(f"- 用户问题：{item.get('question', '')}")
            lines.append(f"  上一轮SQL：{item.get('sql', '')}")
        lines.append(f"当前用户问题：{state['question']}")
        lines.append("请以当前用户问题为目标，必要时继承历史查询中的筛选、表和指标语义。")
        return "\n".join(lines)

    # ---- 节点 1: 获取 schema ----
    @tracer.start_as_current_span("verisql.schema.retrieve")
    def get_schema_context(state: AgentState) -> dict:
        if is_cancelled(state):
            return {"cancelled": True}
        span = trace.get_current_span()
        if state.get("schema_context"):
            span.set_attribute("verisql.schema.cached_in_state", True)
            return {}
        if initial_schema_rag and schema_retriever:
            result = schema_retriever.retrieve(
                state["db_path"],
                contextual_question(state),
                reason="initial schema retrieval",
            )
            events = list(state.get("retrieval_events", []))
            event = result.to_event()
            event["stage"] = "initial"
            events.append(event)
            set_span_attributes(
                span,
                {
                    "verisql.schema.mode": "rag",
                    "verisql.schema.table_count": len(result.tables),
                    "verisql.schema.bridge_table_count": len(result.bridge_tables),
                    "verisql.schema.tables": result.tables,
                },
            )
            return {
                "schema_context": result.context,
                "retrieval_events": events,
            }
        if database:
            schema = database.get_schema()
            span.set_attribute("verisql.schema.mode", "full")
            span.set_attribute("verisql.schema.table_count", len(schema))
            lines = ["数据库中允许访问的表和字段："]
            for table_name, columns in sorted(schema.items()):
                lines.append(f"表 `{table_name}`:")
                lines.extend(f"  - `{column}`" for column in sorted(columns))
            return {"schema_context": "\n".join(lines)}
        # 第一版直接返回静态 schema；后续换成向量检索时，这里改成
        # 根据 state["question"] 去检索最相关的表，返回检索结果拼成的文本
        return {"schema_context": DEMO_SCHEMA_CONTEXT}

    # ---- 节点 2: 生成 SQL ----
    @tracer.start_as_current_span("verisql.llm.generate_sql")
    def generate_sql(state: AgentState) -> dict:
        if is_cancelled(state):
            return {"cancelled": True}
        span = trace.get_current_span()
        set_span_attributes(
            span,
            {
                "gen_ai.operation.name": "generate_sql",
                "gen_ai.request.model": getattr(llm, "model", type(llm).__name__),
                "verisql.retry_count": state.get("retry_count", 0),
                "verisql.history_turns": len(state.get("conversation_history", [])),
            },
        )
        schema_context = state["schema_context"]
        if state.get("retrieval_context"):
            schema_context += "\n\n" + state["retrieval_context"]
        sql = llm.generate_sql(
            question=contextual_question(state),
            schema_context=schema_context,
            error_history=state.get("error_history", []),
        )
        generation_trace = dict(getattr(llm, "last_trace", {}) or {})
        previous_trace = dict(state.get("generation_trace", {}) or {})
        generation_trace["model_calls"] = int(previous_trace.get("model_calls", 0)) + int(
            generation_trace.get("model_calls", 0)
        )
        previous_usage = dict(previous_trace.get("token_usage", {}) or {})
        current_usage = dict(generation_trace.get("token_usage", {}) or {})
        generation_trace["token_usage"] = {
            key: int(previous_usage.get(key, 0)) + int(current_usage.get(key, 0))
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
        set_span_attributes(
            span,
            {
                "verisql.etc.triggered": bool(generation_trace.get("etc_triggered")),
                "verisql.llm.logprobs_available": bool(generation_trace.get("logprobs_available")),
            },
        )
        return {"current_sql": sql, "generation_trace": generation_trace}

    def route_after_generate(state: AgentState) -> str:
        if state.get("cancelled") or is_cancelled(state):
            return "cancel_query"
        trace = state.get("generation_trace", {})
        if schema_retriever and trace.get("etc_triggered") and not state.get("retrieval_context"):
            return "dynamic_retrieve"
        return "validate_sql"

    @tracer.start_as_current_span("verisql.schema.dynamic_retrieve")
    def dynamic_retrieve(state: AgentState) -> dict:
        if is_cancelled(state):
            return {"cancelled": True}
        generation_trace = state.get("generation_trace", {})
        prefix = generation_trace.get("trigger_prefix") or state.get("current_sql", "")
        result = schema_retriever.retrieve(
            state["db_path"], contextual_question(state), sql_prefix=prefix
        )
        events = list(state.get("retrieval_events", []))
        event = result.to_event()
        event.update(
            {
                "stage": "dynamic",
                "trigger_index": generation_trace.get("trigger_index"),
                "sql_prefix": prefix,
            }
        )
        events.append(event)
        span = trace.get_current_span()
        span.set_attribute("verisql.schema.table_count", len(result.tables))
        set_span_attributes(span, {"verisql.schema.tables": result.tables})
        return {"retrieval_context": result.context, "retrieval_events": events}

    @tracer.start_as_current_span("verisql.schema.corrective_retrieve")
    def corrective_retrieve(state: AgentState) -> dict:
        """Expand schema evidence after a schema/value/join-related failure."""
        if is_cancelled(state) or not schema_retriever:
            return {"cancelled": True} if is_cancelled(state) else {}
        error = state.get("safety_reason") or state.get("execution_error", "")
        query = f"{contextual_question(state)}\n失败 SQL: {state.get('current_sql', '')}\n错误: {error}"
        result = schema_retriever.retrieve(
            state["db_path"], query, sql_prefix=state.get("current_sql", ""),
            top_k=6, reason="error-directed corrective retrieval",
        )
        events = list(state.get("retrieval_events", []))
        event = result.to_event()
        event.update({"stage": "corrective", "error_type": state.get("validation_error_type") or state.get("execution_error_type")})
        events.append(event)
        previous = state.get("retrieval_context", "")
        combined = f"{previous}\n\n纠错检索补充：\n{result.context}".strip()
        return {"retrieval_context": combined, "retrieval_events": events}

    # ---- 节点 3: 安全校验 ----
    @tracer.start_as_current_span("verisql.sql.validate")
    def validate_sql(state: AgentState) -> dict:
        if is_cancelled(state):
            return {"cancelled": True}
        result = validate_sql_ast(
            state["current_sql"],
            db_path=None if database else state["db_path"],
            dialect=database.dialect if database else "sqlite",
            schema=database.get_schema() if database else None,
        )
        span = trace.get_current_span()
        set_span_attributes(
            span,
            {
                "verisql.sql.valid": result.is_valid,
                "verisql.sql.error_type": result.error_type,
                "verisql.sql.table_count": len(result.used_tables),
                "verisql.sql.tables": result.used_tables,
            },
        )
        return {
            "safety_check_passed": result.is_valid,
            "safety_reason": result.reason,
            "validation_error_type": result.error_type,
            "ast_validation": result.to_dict(),
        }

    # ---- 节点 4: EXPLAIN 成本检查 ----
    @tracer.start_as_current_span("verisql.sql.cost_check")
    def check_cost(state: AgentState) -> dict:
        if is_cancelled(state):
            return {"cancelled": True}
        target = database or SQLiteDatabase(state["db_path"])
        estimate = target.explain(state["current_sql"], timeout_sec=min(3.0, execute_timeout_sec))
        blocked_reasons = []
        if estimate.estimated_cost is not None and estimate.estimated_cost > cost_max_estimated_cost:
            blocked_reasons.append(f"估算成本 {estimate.estimated_cost:g} 超过阈值 {cost_max_estimated_cost:g}")
        if estimate.estimated_rows is not None and estimate.estimated_rows > cost_max_estimated_rows:
            blocked_reasons.append(f"估算行数 {estimate.estimated_rows} 超过阈值 {cost_max_estimated_rows}")
        if estimate.full_scans > cost_max_full_scans:
            blocked_reasons.append(f"全表扫描 {estimate.full_scans} 次超过阈值 {cost_max_full_scans}")
        estimate.allowed = not (cost_enforce and blocked_reasons)
        estimate.warnings.extend(blocked_reasons)
        result = estimate.to_dict()
        result["enforced"] = cost_enforce
        span = trace.get_current_span()
        set_span_attributes(span, {
            "verisql.cost.available": estimate.available,
            "verisql.cost.allowed": estimate.allowed,
            "verisql.cost.level": estimate.level,
            "verisql.cost.estimated_cost": estimate.estimated_cost,
            "verisql.cost.estimated_rows": estimate.estimated_rows,
            "verisql.cost.full_scans": estimate.full_scans,
        })
        return {
            "cost_check": result,
            "execution_error": "; ".join(blocked_reasons) if not estimate.allowed else "",
            "execution_error_type": "cost" if not estimate.allowed else "",
        }

    # ---- 节点 5: 执行 SQL ----
    @tracer.start_as_current_span("verisql.sql.execute")
    def execute_sql_node(state: AgentState) -> dict:
        if is_cancelled(state):
            return {
                "cancelled": True, "execution_success": False,
                "execution_error": "query cancelled by user",
                "execution_error_type": "cancelled",
            }
        if database:
            result = database.execute(
                state["current_sql"],
                timeout_sec=execute_timeout_sec,
                max_rows=max_result_rows,
                cancel_event=cancellation_event(state),
            )
        else:
            result = run_sql(
                state["db_path"],
                state["current_sql"],
                timeout_sec=execute_timeout_sec,
                max_rows=max_result_rows,
                cancel_event=cancellation_event(state),
            )
        span = trace.get_current_span()
        set_span_attributes(
            span,
            {
                "db.system.name": database.dialect if database else "sqlite",
                "verisql.sql.execution_success": result.success,
                "verisql.sql.result_row_count": len(result.rows),
                "verisql.sql.result_truncated": result.truncated,
                "verisql.sql.execution_error_type": result.error_type,
            },
        )
        return {
            "execution_success": result.success,
            "execution_rows": result.rows,
            "execution_columns": result.columns,
            "execution_error": result.error or "",
            "execution_error_type": result.error_type or "",
            "execution_truncated": result.truncated,
        }

    # ---- 节点 6: 反思（路由节点，同时负责把这次失败记进 error_history）----
    @tracer.start_as_current_span("verisql.agent.reflect")
    def reflect(state: AgentState) -> dict:
        if state.get("cancelled") or state.get("execution_error_type") == "cancelled":
            return {"cancelled": True}
        # 如果这一轮本来就是成功的，不需要记录错误，直接透传
        if state.get("safety_check_passed") and state.get("execution_success"):
            return {}

        # 组装本次失败记录，供下一次 generate_sql 参考
        if not state.get("safety_check_passed"):
            record: ErrorRecord = {
                "sql": state["current_sql"],
                "error": state.get("safety_reason", "AST validation failed"),
                "error_type": state.get("validation_error_type", "safety"),
            }
        else:
            record = {
                "sql": state["current_sql"],
                "error": state.get("execution_error", ""),
                "error_type": state.get("execution_error_type", "unknown"),
            }

        history = list(state.get("error_history", []))
        history.append(record)
        return {
            "error_history": history,
            "retry_count": state.get("retry_count", 0) + 1,
        }

    def route_after_validate(state: AgentState) -> str:
        if state.get("cancelled") or is_cancelled(state):
            return "cancel_query"
        return "check_cost" if state["safety_check_passed"] else "reflect"

    def route_after_cost(state: AgentState) -> str:
        if state.get("cancelled") or is_cancelled(state):
            return "cancel_query"
        return "execute_sql" if state.get("cost_check", {}).get("allowed", True) else "reflect"

    def route_after_reflect(state: AgentState) -> str:
        if state.get("cancelled") or is_cancelled(state):
            return "cancel_query"
        # 这一轮本来就是成功的：safety 通过 且 execution 成功
        if state.get("safety_check_passed") and state.get("execution_success"):
            return "generate_answer"
        if state.get("execution_error_type") in {"permission"}:
            return "human_handoff"
        if is_repeated_sql(state):
            return "human_handoff"
        if state.get("retry_count", 0) >= state.get("max_retries", max_retries):
            return "human_handoff"
        error_type = state.get("validation_error_type") or state.get("execution_error_type", "")
        if schema_retriever and error_type in {
            "unknown_table", "unknown_column", "ambiguous_column", "schema", "runtime"
        }:
            return "corrective_retrieve"
        return "generate_sql"

    def is_repeated_sql(state: AgentState) -> bool:
        current = canonicalize_sql(state.get("current_sql", ""))
        if not current:
            return False
        history = [canonicalize_sql(item.get("sql", "")) for item in state.get("error_history", [])]
        if history and history[-1] == current:
            history = history[:-1]
        return current in history

    # ---- 节点 7: 生成自然语言答案 ----
    @tracer.start_as_current_span("verisql.answer.generate")
    def generate_answer(state: AgentState) -> dict:
        rows = state.get("execution_rows", [])
        cols = state.get("execution_columns", [])
        # 简单模板拼接；后续可以换成 LLM 把结果转述成更自然的句子
        if not rows:
            answer = "查询执行成功，但没有符合条件的数据。"
        else:
            col_str = ", ".join(cols)
            row_str = "; ".join(str(r) for r in rows[:5])
            answer = f"查询结果（列: {col_str}）: {row_str}"
        return {
            "final_answer": answer,
            "status": "success",
            "needs_human": False,
            "conversation_history": list(state.get("conversation_history", []))
            + [{
                "question": state["question"],
                "sql": state.get("current_sql", ""),
                "status": "success",
            }],
        }

    # ---- 节点 8: 人工兜底 ----
    @tracer.start_as_current_span("verisql.agent.human_handoff")
    def human_handoff(state: AgentState) -> dict:
        last_error = state["error_history"][-1] if state.get("error_history") else {}
        answer = (
            f"抱歉，尝试了 {state.get('retry_count', 0)} 次仍未能生成有效查询，"
            f"最后一次失败原因：{last_error.get('error', '未知')}。"
            "已转人工处理。"
        )
        return {
            "final_answer": answer,
            "status": "human_handoff",
            "needs_human": True,
            "conversation_history": list(state.get("conversation_history", []))
            + [{
                "question": state["question"],
                "sql": state.get("current_sql", ""),
                "status": "human_handoff",
            }],
        }

    @tracer.start_as_current_span("verisql.agent.cancelled")
    def cancel_query(state: AgentState) -> dict:
        return {
            "cancelled": True,
            "execution_success": False,
            "execution_error": "query cancelled by user",
            "execution_error_type": "cancelled",
            "final_answer": "查询已取消，未继续执行后续步骤。",
            "status": "cancelled",
            "needs_human": False,
        }

    # ---- 组装状态图 ----
    graph = StateGraph(AgentState)
    graph.add_node("get_schema_context", get_schema_context)
    graph.add_node("generate_sql", generate_sql)
    graph.add_node("dynamic_retrieve", dynamic_retrieve)
    graph.add_node("corrective_retrieve", corrective_retrieve)
    graph.add_node("validate_sql", validate_sql)
    graph.add_node("check_cost", check_cost)
    graph.add_node("execute_sql", execute_sql_node)
    graph.add_node("reflect", reflect)
    graph.add_node("generate_answer", generate_answer)
    graph.add_node("human_handoff", human_handoff)
    graph.add_node("cancel_query", cancel_query)

    graph.add_edge(START, "get_schema_context")
    graph.add_conditional_edges(
        "get_schema_context",
        lambda state: "cancel_query" if state.get("cancelled") or is_cancelled(state) else "generate_sql",
        {"generate_sql": "generate_sql", "cancel_query": "cancel_query"},
    )
    graph.add_conditional_edges(
        "generate_sql",
        route_after_generate,
        {"dynamic_retrieve": "dynamic_retrieve", "validate_sql": "validate_sql", "cancel_query": "cancel_query"},
    )
    graph.add_edge("dynamic_retrieve", "generate_sql")
    graph.add_edge("corrective_retrieve", "generate_sql")
    graph.add_conditional_edges(
        "validate_sql",
        route_after_validate,
        {"check_cost": "check_cost", "reflect": "reflect", "cancel_query": "cancel_query"},
    )
    graph.add_conditional_edges(
        "check_cost", route_after_cost,
        {"execute_sql": "execute_sql", "reflect": "reflect", "cancel_query": "cancel_query"},
    )
    graph.add_edge("execute_sql", "reflect")
    graph.add_conditional_edges(
        "reflect",
        route_after_reflect,
        {
            "generate_answer": "generate_answer",
            "generate_sql": "generate_sql",
            "corrective_retrieve": "corrective_retrieve",
            "human_handoff": "human_handoff",
            "cancel_query": "cancel_query",
        },
    )
    graph.add_edge("generate_answer", END)
    graph.add_edge("human_handoff", END)
    graph.add_edge("cancel_query", END)

    return graph.compile(checkpointer=checkpointer)


def canonicalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().rstrip(";").lower())
