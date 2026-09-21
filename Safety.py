"""
safety.py
---------
SQL 安全检查。这是训练和推理阶段共用的第一道防线，独立于 executor 的
只读连接（防御要做两层：这里挡掉不该跑的 SQL，executor 里再用只读连接
兜底，防止 safety.py 漏检）。

检测的危险类型：
  1. 非 SELECT 的 DML/DDL：INSERT / UPDATE / DELETE / DROP / ALTER /
     TRUNCATE / CREATE / REPLACE
  2. PRAGMA / ATTACH：可能被用来读写数据库文件之外的资源
  3. 堆叠语句（stacked queries）：例如 "SELECT 1; DROP TABLE x"，
     只允许提交恰好一条语句
  4. UPDATE / DELETE 缺少 WHERE 子句（虽然第 1 条已经整体拦截了
     UPDATE/DELETE，这里单独保留判断函数，方便以后如果开放写权限时复用）
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlparse
from sqlparse.sql import Statement
from sqlparse.tokens import DDL, DML, Keyword

_DANGEROUS_DDL_DML = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "TRUNCATE",
    "CREATE",
    "REPLACE",
}
_DANGEROUS_KEYWORDS = {"PRAGMA", "ATTACH", "DETACH", "VACUUM"}


@dataclass
class SafetyCheckResult:
    is_safe: bool
    reason: str = ""
    statement_type: str = "UNKNOWN"


def _get_statement_type(stmt: Statement) -> str:
    stmt_type = stmt.get_type()  # sqlparse 对 SELECT/INSERT/UPDATE/DELETE 等有内置识别
    if stmt_type and stmt_type != "UNKNOWN":
        return stmt_type.upper()

    # sqlparse 对 DROP/ALTER/TRUNCATE 等有时归类不准，兜底扫 token
    for token in stmt.flatten():
        if token.ttype in (DDL, DML) or token.ttype is Keyword:
            val = token.value.upper()
            if val in _DANGEROUS_DDL_DML or val in _DANGEROUS_KEYWORDS:
                return val
    return "UNKNOWN"


def check_sql_safety(sql: str) -> SafetyCheckResult:
    """
    对一条 SQL 做安全检查，返回是否安全及原因。
    永远不抛异常——解析失败本身也会被判定为不安全，交给上层当作
    safety violation 处理（而不是当成 syntax error 静默放过）。
    """
    if not sql or not sql.strip():
        return SafetyCheckResult(is_safe=False, reason="empty SQL")

    try:
        statements = sqlparse.parse(sql)
    except Exception as exc:  # noqa: BLE001
        return SafetyCheckResult(is_safe=False, reason=f"failed to parse: {exc}")

    # 过滤掉纯空白/纯注释的“语句”，只统计真正有内容的语句数
    real_statements = [s for s in statements if s.token_first(skip_cm=True) is not None]

    if len(real_statements) == 0:
        return SafetyCheckResult(is_safe=False, reason="no valid statement found")

    if len(real_statements) > 1:
        return SafetyCheckResult(
            is_safe=False,
            reason=f"stacked queries not allowed ({len(real_statements)} statements)",
        )

    stmt = real_statements[0]
    stmt_type = _get_statement_type(stmt)

    if stmt_type in _DANGEROUS_DDL_DML:
        return SafetyCheckResult(
            is_safe=False,
            reason=f"disallowed statement type: {stmt_type}",
            statement_type=stmt_type,
        )

    # 关键词兜底扫描（防止 sqlparse 漏判，比如混在子查询/CTE 里的危险关键词）
    upper_sql = sql.upper()
    for kw in _DANGEROUS_KEYWORDS:
        if kw in upper_sql:
            return SafetyCheckResult(
                is_safe=False,
                reason=f"disallowed keyword: {kw}",
                statement_type=stmt_type,
            )

    # 注意：这里故意不做"只白名单放行 SELECT"。
    # 原因：sqlparse 对拼写错误（如 "SELEC name ..."）或不常见但无害的写法
    # （如 "WITH cte AS (...) SELECT ..."、"EXPLAIN SELECT ..."）经常无法
    # 归类出准确的 statement_type，落到 UNKNOWN。如果这里直接拒绝 UNKNOWN，
    # 会把"手误拼错关键字"这种应该走语法错误路径（reward=0）的情况，
    # 错误地判成安全违规（reward=-1），惩罚力度不对，污染训练信号。
    #
    # 真正的安全边界由两层防线保证：
    #   1. 上面已经按类型/关键词拦截了所有已知危险操作
    #   2. executor.py 用只读连接打开数据库，任何写操作在数据库层面
    #      物理上就会失败（无论 safety.py 是否识别出来）
    # 因此对于既不危险、也不是标准 SELECT 的情况，交给 executor 去尝试
    # 执行——能跑通就是合法的只读查询，跑不通就自然落入 syntax/runtime
    # error 分支，reward=0，而不是被误伤为 -1。
    return SafetyCheckResult(is_safe=True, statement_type=stmt_type)


def has_missing_where(sql: str) -> bool:
    """
    判断 UPDATE / DELETE 语句是否缺少 WHERE 子句。
    当前 pipeline 里 UPDATE/DELETE 已经被 check_sql_safety 整体拦截，
    这个函数保留给未来若开放"允许受限写操作"场景复用。
    """
    statements = sqlparse.parse(sql)
    if not statements:
        return False
    stmt = statements[0]
    stmt_type = _get_statement_type(stmt)
    if stmt_type not in {"UPDATE", "DELETE"}:
        return False
    return "WHERE" not in sql.upper()