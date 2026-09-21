"""Database runtime adapters used by the online Agent.

SQLite remains the zero-configuration demo backend. PostgreSQL uses a bounded
connection pool and wraps every generated query in an explicit read-only
transaction with a server-side statement timeout.
"""

from __future__ import annotations

import time
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from Config import Settings
from Demo_db import DB_PATH
from Executor import ExecutionResult, execute_sql
from Sql_validator import load_sqlite_schema


@dataclass
class CostEstimate:
    """Portable, best-effort EXPLAIN result used before executing generated SQL."""

    available: bool = True
    allowed: bool = True
    level: str = "low"
    estimated_cost: float | None = None
    estimated_rows: int | None = None
    full_scans: int = 0
    warnings: list[str] = field(default_factory=list)
    plan: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "allowed": self.allowed,
            "level": self.level,
            "estimated_cost": self.estimated_cost,
            "estimated_rows": self.estimated_rows,
            "full_scans": self.full_scans,
            "warnings": self.warnings,
            "plan": self.plan,
        }


class AgentDatabase(Protocol):
    dialect: str
    display_name: str

    def open(self) -> None: ...
    def close(self) -> None: ...
    def get_schema(self) -> dict[str, set[str]]: ...
    def explain(self, sql: str, timeout_sec: float = 3.0) -> CostEstimate: ...
    def execute(self, sql: str, timeout_sec: float, max_rows: int, cancel_event=None) -> ExecutionResult: ...


class SQLiteDatabase:
    dialect = "sqlite"

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.display_name = Path(path).name

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def get_schema(self) -> dict[str, set[str]]:
        return load_sqlite_schema(self.path)

    def explain(self, sql: str, timeout_sec: float = 3.0) -> CostEstimate:
        try:
            uri = f"file:{self.path}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=timeout_sec) as conn:
                rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
            plan = [str(row[3]) for row in rows]
            full_scans = sum(
                1 for detail in plan
                if "SCAN " in detail.upper() and "USING INDEX" not in detail.upper()
                and "USING COVERING INDEX" not in detail.upper()
            )
            temp_ops = sum("USE TEMP B-TREE" in detail.upper() for detail in plan)
            warnings = []
            if full_scans:
                warnings.append(f"执行计划包含 {full_scans} 次未命中索引的全表扫描")
            if temp_ops:
                warnings.append(f"执行计划包含 {temp_ops} 次临时 B-Tree 排序/聚合")
            level = "high" if full_scans >= 3 else "medium" if full_scans or temp_ops else "low"
            return CostEstimate(level=level, full_scans=full_scans, warnings=warnings, plan=plan[:20])
        except Exception as exc:
            return CostEstimate(available=False, level="unknown", warnings=[f"EXPLAIN 不可用：{type(exc).__name__}"])

    def execute(self, sql: str, timeout_sec: float, max_rows: int, cancel_event=None) -> ExecutionResult:
        return execute_sql(
            self.path, sql, timeout_sec=timeout_sec, max_rows=max_rows,
            cancel_event=cancel_event,
        )


class PostgreSQLDatabase:
    dialect = "postgres"

    def __init__(
        self,
        dsn: str,
        schemas: tuple[str, ...] = ("public",),
        min_size: int = 1,
        max_size: int = 10,
        pool_timeout_sec: float = 5.0,
        max_lifetime_sec: float = 1800.0,
        pool=None,
    ):
        if not dsn and pool is None:
            raise ValueError("DATABASE_URL is required for the PostgreSQL backend")
        if min_size < 0 or max_size < 1 or min_size > max_size:
            raise ValueError("invalid PostgreSQL pool size configuration")
        self.dsn = dsn
        self.schemas = schemas
        self.pool_timeout_sec = pool_timeout_sec
        self.display_name = "PostgreSQL"
        self._schema_cache: dict[str, set[str]] | None = None
        if pool is not None:
            self.pool = pool
        else:
            from psycopg_pool import ConnectionPool

            self.pool = ConnectionPool(
                conninfo=dsn,
                min_size=min_size,
                max_size=max_size,
                timeout=pool_timeout_sec,
                max_lifetime=max_lifetime_sec,
                kwargs={
                    "autocommit": False,
                    "options": "-c default_transaction_read_only=on",
                },
                open=False,
                name="verisql-agent",
            )

    def open(self) -> None:
        self.pool.open(wait=True, timeout=self.pool_timeout_sec)

    def close(self) -> None:
        self.pool.close()

    def get_schema(self, refresh: bool = False) -> dict[str, set[str]]:
        if self._schema_cache is not None and not refresh:
            return self._schema_cache
        query = """
            SELECT table_schema, table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = ANY(%s)
            ORDER BY table_schema, table_name, ordinal_position
        """
        schema: dict[str, set[str]] = {}
        with self.pool.connection(timeout=self.pool_timeout_sec) as conn:
            try:
                conn.execute("BEGIN READ ONLY")
                rows = conn.execute(query, (list(self.schemas),)).fetchall()
                for schema_name, table_name, column_name in rows:
                    key = f"{schema_name}.{table_name}".lower()
                    schema.setdefault(key, set()).add(str(column_name).lower())
            finally:
                conn.rollback()
        self._schema_cache = schema
        return schema

    def explain(self, sql: str, timeout_sec: float = 3.0) -> CostEstimate:
        try:
            with self.pool.connection(timeout=self.pool_timeout_sec) as conn:
                try:
                    conn.execute("BEGIN READ ONLY")
                    conn.execute("SELECT set_config('statement_timeout', %s, true)", (str(max(1, int(timeout_sec * 1000))),))
                    raw = conn.execute(f"EXPLAIN (FORMAT JSON, COSTS TRUE) {sql}").fetchone()[0]
                    document = raw if isinstance(raw, list) else json.loads(raw)
                    plan = document[0]["Plan"]
                    cost = float(plan.get("Total Cost", 0))
                    rows = int(plan.get("Plan Rows", 0))
                    warnings = []
                    if cost >= 100000:
                        warnings.append(f"PostgreSQL 估算成本较高：{cost:g}")
                    return CostEstimate(
                        level="high" if cost >= 100000 else "medium" if cost >= 10000 else "low",
                        estimated_cost=cost, estimated_rows=rows, warnings=warnings,
                        plan=[json.dumps(plan, ensure_ascii=False)[:4000]],
                    )
                finally:
                    conn.rollback()
        except Exception as exc:
            return CostEstimate(available=False, level="unknown", warnings=[f"EXPLAIN 不可用：{type(exc).__name__}"])

    def execute(self, sql: str, timeout_sec: float, max_rows: int, cancel_event=None) -> ExecutionResult:
        start = time.monotonic()
        conn = None
        try:
            with self.pool.connection(timeout=self.pool_timeout_sec) as conn:
                try:
                    conn.execute("BEGIN READ ONLY")
                    timeout_ms = max(1, int(timeout_sec * 1000))
                    conn.execute("SELECT set_config('statement_timeout', %s, true)", (str(timeout_ms),))
                    stop_watch = threading.Event()
                    watcher = None
                    if cancel_event is not None:
                        def watch_cancel():
                            while not stop_watch.wait(0.05):
                                if cancel_event.is_set():
                                    try:
                                        conn.cancel()
                                    except Exception:
                                        pass
                                    return
                        watcher = threading.Thread(target=watch_cancel, daemon=True)
                        watcher.start()
                    cursor = conn.execute(sql)
                    columns = [column.name for column in cursor.description] if cursor.description else []
                    rows = cursor.fetchmany(max_rows + 1)
                    truncated = len(rows) > max_rows
                    result = ExecutionResult(
                        success=True,
                        rows=list(rows[:max_rows]),
                        columns=columns,
                        elapsed_sec=time.monotonic() - start,
                        truncated=truncated,
                    )
                    stop_watch.set()
                    return result
                finally:
                    conn.rollback()
        except Exception as exc:  # database errors must not crash the Agent graph
            return ExecutionResult(
                success=False,
                error="query cancelled by user" if cancel_event is not None and cancel_event.is_set() else str(exc),
                error_type="cancelled" if cancel_event is not None and cancel_event.is_set() else _classify_postgres_error(exc),
                elapsed_sec=time.monotonic() - start,
            )


class MySQLDatabase:
    """MySQL adapter using read-only sessions and bounded connections."""

    dialect = "mysql"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        max_size: int = 10,
        connect_timeout_sec: float = 5.0,
        ssl: bool = False,
    ):
        if not host or not database or not user:
            raise ValueError("MySQL host, database and user are required")
        self.host, self.port, self.database_name = host, port, database
        self.user, self.password, self.ssl = user, password, ssl
        self.max_size = max(1, max_size)
        self.connect_timeout_sec = connect_timeout_sec
        self.display_name = f"MySQL · {database}"
        self._schema_cache: dict[str, set[str]] | None = None
        self._semaphore = None

    def open(self) -> None:
        import threading

        self._semaphore = threading.BoundedSemaphore(self.max_size)
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")

    def close(self) -> None:
        return None

    @contextmanager
    def _connection(self):
        import pymysql

        if self._semaphore is None:
            import threading
            self._semaphore = threading.BoundedSemaphore(self.max_size)
        acquired = self._semaphore.acquire(timeout=self.connect_timeout_sec)
        if not acquired:
            raise TimeoutError("MySQL connection limit wait timed out")
        conn = None
        try:
            conn = pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database_name,
                connect_timeout=max(1, int(self.connect_timeout_sec)),
                read_timeout=max(1, int(self.connect_timeout_sec)),
                write_timeout=max(1, int(self.connect_timeout_sec)),
                charset="utf8mb4",
                autocommit=False,
                ssl={} if self.ssl else None,
            )
            yield conn
        finally:
            if conn is not None:
                conn.close()
            self._semaphore.release()

    def get_schema(self, refresh: bool = False) -> dict[str, set[str]]:
        if self._schema_cache is not None and not refresh:
            return self._schema_cache
        schema: dict[str, set[str]] = {}
        with self._connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """SELECT table_name, column_name FROM information_schema.columns
                       WHERE table_schema = %s ORDER BY table_name, ordinal_position""",
                    (self.database_name,),
                )
                for table_name, column_name in cursor.fetchall():
                    schema.setdefault(str(table_name).lower(), set()).add(str(column_name).lower())
            conn.rollback()
        self._schema_cache = schema
        return schema

    def explain(self, sql: str, timeout_sec: float = 3.0) -> CostEstimate:
        try:
            with self._connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SET SESSION TRANSACTION READ ONLY")
                    cursor.execute("EXPLAIN FORMAT=JSON " + sql)
                    raw = cursor.fetchone()[0]
                    document = json.loads(raw) if isinstance(raw, str) else raw
                    cost = _find_number(document, "query_cost")
                    rows = _find_number(document, "rows_examined_per_scan")
                    warnings = [f"MySQL 估算成本较高：{cost:g}"] if cost is not None and cost >= 100000 else []
                    return CostEstimate(
                        level="high" if cost is not None and cost >= 100000 else "medium" if cost is not None and cost >= 10000 else "low",
                        estimated_cost=cost, estimated_rows=int(rows) if rows is not None else None,
                        warnings=warnings, plan=[json.dumps(document, ensure_ascii=False)[:4000]],
                    )
        except Exception as exc:
            return CostEstimate(available=False, level="unknown", warnings=[f"EXPLAIN 不可用：{type(exc).__name__}"])

    def execute(self, sql: str, timeout_sec: float, max_rows: int, cancel_event=None) -> ExecutionResult:
        if cancel_event is not None and cancel_event.is_set():
            return ExecutionResult(success=False, error="query cancelled by user", error_type="cancelled")
        start = time.monotonic()
        try:
            with self._connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SET SESSION TRANSACTION READ ONLY")
                    cursor.execute("SET SESSION MAX_EXECUTION_TIME = %s", (max(1, int(timeout_sec * 1000)),))
                    cursor.execute("START TRANSACTION READ ONLY")
                    cursor.execute(sql)
                    columns = [column[0] for column in cursor.description] if cursor.description else []
                    rows = list(cursor.fetchmany(max_rows + 1))
                    conn.rollback()
                    return ExecutionResult(
                        success=True,
                        rows=rows[:max_rows],
                        columns=columns,
                        elapsed_sec=time.monotonic() - start,
                        truncated=len(rows) > max_rows,
                    )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                error=str(exc),
                error_type=_classify_mysql_error(exc),
                elapsed_sec=time.monotonic() - start,
            )


def _find_number(value, key: str) -> float | None:
    if isinstance(value, dict):
        if key in value:
            try:
                return float(value[key])
            except (TypeError, ValueError):
                pass
        for child in value.values():
            found = _find_number(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_number(child, key)
            if found is not None:
                return found
    return None


def _classify_mysql_error(exc: Exception) -> str:
    code = getattr(exc, "args", [None])[0]
    if code in {1142, 1227, 1290}:
        return "permission"
    if code in {1054, 1146}:
        return "schema"
    if code in {1064}:
        return "syntax"
    if code in {1205, 1317, 3024}:
        return "timeout"
    return "runtime"


def _classify_postgres_error(exc: Exception) -> str:
    try:
        from psycopg import errors

        if isinstance(exc, errors.QueryCanceled):
            return "timeout"
        if isinstance(exc, (errors.UndefinedTable, errors.UndefinedColumn, errors.AmbiguousColumn)):
            return "schema"
        if isinstance(exc, (errors.InsufficientPrivilege, errors.ReadOnlySqlTransaction)):
            return "permission"
        if isinstance(exc, errors.SyntaxError):
            return "syntax"
    except ImportError:
        pass
    message = str(exc).lower()
    if "timeout" in message or "canceling statement" in message:
        return "timeout"
    if "permission" in message or "read-only" in message or "readonly" in message:
        return "permission"
    return "runtime"


def build_database(settings: Settings) -> AgentDatabase:
    if settings.database_backend == "sqlite":
        return SQLiteDatabase(DB_PATH)
    return PostgreSQLDatabase(
        dsn=settings.database_url,
        schemas=settings.database_schemas,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        pool_timeout_sec=settings.db_pool_timeout_sec,
        max_lifetime_sec=settings.db_pool_max_lifetime_sec,
    )
