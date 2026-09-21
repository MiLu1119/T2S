"""Per-user workbench history and short-lived paginated result storage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from Audit import redact_secrets


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


class WorkbenchStore:
    def __init__(self, path: str | Path, result_ttl_minutes: int = 1440):
        self.path = Path(path)
        self.result_ttl_minutes = max(5, result_ttl_minutes)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS workbench_sessions (
                    user_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    data_source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, thread_id, data_source_id)
                );
                CREATE INDEX IF NOT EXISTS idx_workbench_sessions_user_updated
                    ON workbench_sessions(user_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS workbench_queries (
                    request_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    data_source_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL DEFAULT '',
                    sql_text TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id, thread_id, data_source_id)
                      REFERENCES workbench_sessions(user_id, thread_id, data_source_id)
                      ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_workbench_queries_session
                    ON workbench_queries(user_id, thread_id, data_source_id, created_at);
                CREATE TABLE IF NOT EXISTS workbench_results (
                    request_id TEXT PRIMARY KEY REFERENCES workbench_queries(request_id) ON DELETE CASCADE,
                    columns_json TEXT NOT NULL,
                    rows_json TEXT NOT NULL,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT NOT NULL
                );
                """
            )
        self.prune()

    def close(self) -> None:
        return None

    def prune(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM workbench_results WHERE expires_at < ?", (_iso(),)
            )
        return max(0, cursor.rowcount)

    def save_query(
        self, *, request_id: str, user_id: str, thread_id: str,
        data_source_id: str, question: str, answer: str, sql: str,
        status: str, columns: list[str], rows: list[list[Any]],
        truncated: bool, latency_ms: int,
    ) -> None:
        now = _iso()
        safe_question = redact_secrets(question)[:2000]
        title = safe_question[:60] or "新会话"
        expires = _iso(_now() + timedelta(minutes=self.result_ttl_minutes))
        serialized_rows = json.dumps(rows, ensure_ascii=False, default=str)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO workbench_sessions(
                   user_id, thread_id, data_source_id, title, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, thread_id, data_source_id)
                   DO UPDATE SET updated_at = excluded.updated_at""",
                (user_id, thread_id, data_source_id, title, now, now),
            )
            conn.execute(
                """INSERT OR REPLACE INTO workbench_queries(
                   request_id, user_id, thread_id, data_source_id, question,
                   answer, sql_text, status, row_count, latency_ms, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, user_id, thread_id, data_source_id, safe_question,
                 redact_secrets(answer), redact_secrets(sql), status, len(rows),
                 max(0, latency_ms), now),
            )
            conn.execute(
                """INSERT OR REPLACE INTO workbench_results(
                   request_id, columns_json, rows_json, truncated, expires_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (request_id, json.dumps(columns, ensure_ascii=False), serialized_rows,
                 int(truncated), expires),
            )

    def list_sessions(self, user_id: str, data_source_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        params: list[Any] = [user_id]
        where = "WHERE s.user_id = ?"
        if data_source_id:
            where += " AND s.data_source_id = ?"
            params.append(data_source_id)
        params.append(min(max(1, limit), 100))
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT s.*, COUNT(q.request_id) AS query_count,
                    MAX(q.status) FILTER (WHERE q.created_at = (
                      SELECT MAX(q2.created_at) FROM workbench_queries q2
                      WHERE q2.user_id=s.user_id AND q2.thread_id=s.thread_id
                        AND q2.data_source_id=s.data_source_id
                    )) AS last_status
                    FROM workbench_sessions s LEFT JOIN workbench_queries q
                      ON q.user_id=s.user_id AND q.thread_id=s.thread_id
                     AND q.data_source_id=s.data_source_id
                    {where} GROUP BY s.user_id, s.thread_id, s.data_source_id
                    ORDER BY s.updated_at DESC LIMIT ?""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def rename_session(self, user_id: str, thread_id: str, data_source_id: str, title: str) -> dict[str, Any]:
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE workbench_sessions SET title = ?, updated_at = ?
                   WHERE user_id = ? AND thread_id = ? AND data_source_id = ?""",
                (title[:100], _iso(), user_id, thread_id, data_source_id),
            )
            if not cursor.rowcount:
                raise KeyError(thread_id)
            row = conn.execute(
                "SELECT * FROM workbench_sessions WHERE user_id=? AND thread_id=? AND data_source_id=?",
                (user_id, thread_id, data_source_id),
            ).fetchone()
        return dict(row)

    def delete_session(self, user_id: str, thread_id: str, data_source_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM workbench_sessions WHERE user_id=? AND thread_id=? AND data_source_id=?",
                (user_id, thread_id, data_source_id),
            )
        return cursor.rowcount > 0

    def list_queries(self, user_id: str, thread_id: str, data_source_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM workbench_sessions WHERE user_id=? AND thread_id=? AND data_source_id=?",
                (user_id, thread_id, data_source_id),
            ).fetchone()
            if not exists:
                raise KeyError(thread_id)
            rows = conn.execute(
                """SELECT request_id, question, answer, sql_text AS sql, status,
                   row_count, latency_ms, created_at FROM workbench_queries
                   WHERE user_id=? AND thread_id=? AND data_source_id=?
                   ORDER BY created_at""",
                (user_id, thread_id, data_source_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_result(self, user_id: str, request_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT r.*, q.thread_id, q.data_source_id, q.question, q.answer,
                   q.sql_text AS sql, q.status, q.latency_ms
                   FROM workbench_results r JOIN workbench_queries q ON q.request_id=r.request_id
                   WHERE r.request_id=? AND q.user_id=? AND r.expires_at >= ?""",
                (request_id, user_id, _iso()),
            ).fetchone()
        if not row:
            raise KeyError(request_id)
        result = dict(row)
        result["columns"] = json.loads(result.pop("columns_json"))
        result["rows"] = json.loads(result.pop("rows_json"))
        result["truncated"] = bool(result["truncated"])
        return result

