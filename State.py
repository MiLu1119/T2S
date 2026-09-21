"""
state.py
--------
LangGraph 状态机的 State 定义。

State 是贯穿整个 Agent 流程、在各节点之间传递的"共享内存"，每个节点
读取需要的字段、更新自己负责的字段，交给下一个节点。这里字段的设计
直接对应后面"多轮对话"要保留的信息（conversation_history 等），
第一版先只用其中一部分（单轮问答），多轮能力是在这个 State 基础上
自然扩展的，不需要重新设计。
"""

from __future__ import annotations

from typing import Optional, TypedDict, Any


class ErrorRecord(TypedDict):
    """记录一次失败尝试，供 Reflection / Refinement 节点参考。"""
    sql: str
    error: str
    error_type: str  # "syntax" | "schema" | "runtime" | "timeout" | "safety" | "mismatch"


class AgentState(TypedDict, total=False):
    # ---- 输入 ----
    question: str                     # 用户的自然语言问题
    db_path: str                      # 目标数据库文件路径

    # ---- Schema ----
    schema_context: str               # 喂给 LLM 的 schema 描述文本
    retrieval_context: str            # ETC 触发后按需检索到的 schema/value 上下文
    retrieval_events: list[dict]      # 动态检索轨迹，供 UI 和评测展示
    generation_trace: dict            # token entropy 与 ETC 触发信息
    conversation_history: list[dict]  # 已完成轮次的 question/sql/status 摘要

    # ---- SQL 生成/校验/执行的当前状态 ----
    current_sql: str                  # 当前这一轮生成的 SQL
    safety_check_passed: bool         # 是否通过安全检查
    safety_reason: str                # 安全检查未通过的原因
    ast_validation: dict              # SQLGlot AST 校验详情
    validation_error_type: str        # syntax / unknown_table / unknown_column / unsafe_statement
    execution_success: bool           # 是否执行成功
    execution_rows: list              # 执行结果（成功时）
    execution_columns: list           # 结果列名
    execution_error: str              # 执行失败时的报错信息
    execution_error_type: str         # 失败类型分类
    execution_truncated: bool         # 返回行是否达到上限并被截断
    cost_check: dict                  # EXPLAIN 的跨数据库成本估算与门禁结论
    request_id: str                   # 单次运行 ID，用于查找进程内取消标记
    cancelled: bool                   # 用户是否请求取消

    # ---- 重试控制 ----
    retry_count: int                  # 已经重试了几次
    max_retries: int                  # 最大重试次数
    error_history: list[ErrorRecord]  # 历次失败记录，供重写时参考

    # ---- 终态 ----
    final_answer: str                 # 生成给用户的自然语言回答
    needs_human: bool                 # 是否需要转人工
    status: str                       # "success" | "human_handoff" | "in_progress"
