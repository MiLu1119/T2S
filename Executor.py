"""
executor.py
-----------
在 BIRD 风格的 SQLite 数据库上安全执行 SQL。

设计原则：
- 任何异常（语法错误、字段不存在、类型错误、超时……）都必须被捕获，
  绝不能让 GRPO 训练的 rollout 进程崩溃。
- 用一个独立线程 + threading.Event 实现超时控制，因为 sqlite3 的
  Python 绑定不支持原生的“执行超时中断”，必须靠 connection.interrupt()
  从另一个线程打断。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ExecutionResult:
    success: bool
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    error: Optional[str] = None
    error_type: Optional[str] = None  # "syntax" | "runtime" | "timeout" | "unknown"
    elapsed_sec: float = 0.0
    truncated: bool = False


def _classify_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if "attempt to write a readonly database" in msg:
        return "permission"
    if "permission denied" in msg:
        return "permission"
    if "unable to open database file" in msg:
        return "permission"
    if "database is locked" in msg:
        return "runtime"
    if isinstance(exc, sqlite3.OperationalError):
        # sqlite 把语法错误和"字段/表不存在"都归到 OperationalError，
        # 靠关键词粗分一下，方便后续做统计分析。
        if "syntax error" in msg:
            return "syntax"
        if "no such column" in msg or "no such table" in msg:
            return "schema"
        return "runtime"
    if isinstance(exc, sqlite3.Warning):
        return "runtime"
    return "unknown"


def execute_sql(
    db_path: str,
    sql: str,
    timeout_sec: float = 5.0,
    max_rows: int = 10_000,
    cancel_event: threading.Event | None = None,
) -> ExecutionResult:
    """
    在给定的 sqlite 数据库文件上执行一条 SQL，返回结构化结果。

    Args:
        db_path: sqlite 数据库文件路径（BIRD 每个 question 对应一个 db 文件）。
        sql: 待执行的 SQL（模型生成的，或 ground truth）。
        timeout_sec: 执行超时时间，超时视为失败，error_type="timeout"。
        max_rows: 最多取回多少行，防止模型生成的 SQL 返回超大结果集拖垮内存。

    Returns:
        ExecutionResult，永远不会抛出异常。
    """
    result_holder: dict[str, Any] = {}
    conn_holder: dict[str, sqlite3.Connection] = {}

    def _worker():
        try:
            # 只读连接：即使 safety.py 漏判了危险 SQL，写操作在这里也会
            # 直接被 sqlite 拒绝（报错 "attempt to write a readonly database"），
            # 这是第二道防线，不依赖关键词/AST 检测的准确性。
            uri = f"file:{db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            conn_holder["conn"] = conn
            cursor = conn.cursor()
            cursor.execute(sql)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(max_rows + 1)
            truncated = len(rows) > max_rows
            rows = rows[:max_rows]
            result_holder["rows"] = rows
            result_holder["columns"] = columns
            result_holder["truncated"] = truncated
            result_holder["success"] = True
        except Exception as exc:  # noqa: BLE001 - 必须兜住一切异常
            result_holder["success"] = False
            result_holder["error"] = str(exc)
            result_holder["error_type"] = _classify_error(exc)
        finally:
            conn = conn_holder.get("conn")
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    start = time.monotonic()
    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    deadline = start + timeout_sec
    while thread.is_alive() and time.monotonic() < deadline:
        thread.join(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
        if cancel_event is not None and cancel_event.is_set():
            conn = conn_holder.get("conn")
            if conn is not None:
                try:
                    conn.interrupt()
                except Exception:  # noqa: BLE001
                    pass
            return ExecutionResult(
                success=False,
                error="query cancelled by user",
                error_type="cancelled",
                elapsed_sec=time.monotonic() - start,
            )
    elapsed = time.monotonic() - start

    if thread.is_alive():
        # 超时：尝试中断连接，让后台线程能尽快退出（不保证立即生效，
        # 但线程被标记为 daemon，不会阻塞主进程退出）。
        conn = conn_holder.get("conn")
        if conn is not None:
            try:
                conn.interrupt()
            except Exception:  # noqa: BLE001
                pass
        return ExecutionResult(
            success=False,
            error=f"execution timeout after {timeout_sec}s",
            error_type="timeout",
            elapsed_sec=elapsed,
        )

    if not result_holder.get("success"):
        return ExecutionResult(
            success=False,
            error=result_holder.get("error", "unknown error"),
            error_type=result_holder.get("error_type", "unknown"),
            elapsed_sec=elapsed,
        )

    return ExecutionResult(
        success=True,
        rows=result_holder["rows"],
        columns=result_holder["columns"],
        elapsed_sec=elapsed,
        truncated=result_holder.get("truncated", False),
    )
