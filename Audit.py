"""Local, structured audit and feedback storage for the Web runtime."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_ -]?key|token|password|secret)\s*[:=]\s*\S+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~-]+"),
    re.compile(r"ark-[A-Za-z0-9-]{12,}"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact_secrets(value: str) -> str:
    sanitized = value
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized


class AuditStore:
    """SQLite-backed audit store, deliberately separate from the business DB."""

    def __init__(
        self,
        path: str | Path,
        *,
        enabled: bool = True,
        retention_days: int = 30,
        store_question: bool = True,
    ):
        self.path = Path(path)
        self.enabled = enabled
        self.retention_days = max(1, retention_days)
        self.store_question = store_question

    def open(self) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS query_audit (
                    request_id TEXT PRIMARY KEY,
                    thread_id TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    username TEXT NOT NULL,
                    question TEXT,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    database_backend TEXT NOT NULL,
                    schema_context_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sql_text TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    error_types_json TEXT NOT NULL DEFAULT '[]',
                    retrieved_tables_json TEXT NOT NULL DEFAULT '[]',
                    result_row_count INTEGER NOT NULL DEFAULT 0,
                    result_truncated INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_query_audit_created_at
                    ON query_audit(created_at DESC);
                CREATE TABLE IF NOT EXISTS query_feedback (
                    request_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    verdict TEXT NOT NULL CHECK(verdict IN ('correct', 'incorrect')),
                    comment TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (request_id, username),
                    FOREIGN KEY (request_id) REFERENCES query_audit(request_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS resource_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    request_id TEXT,
                    username TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT,
                    resource_name TEXT,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_resource_audit_created_at
                    ON resource_audit(created_at DESC);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(query_audit)")}
            migrations = {
                "thread_id": "TEXT", "trace_id": "TEXT", "prompt_tokens": "INTEGER NOT NULL DEFAULT 0",
                "completion_tokens": "INTEGER NOT NULL DEFAULT 0", "total_tokens": "INTEGER NOT NULL DEFAULT 0",
                "model_calls": "INTEGER NOT NULL DEFAULT 0", "confidence_score": "REAL",
                "confidence_level": "TEXT", "cost_level": "TEXT", "estimated_cost": "REAL",
                "cancelled": "INTEGER NOT NULL DEFAULT 0", "timeline_json": "TEXT NOT NULL DEFAULT '[]'",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE query_audit ADD COLUMN {name} {definition}")
        self.prune()

    def close(self) -> None:
        # Connections are short lived so request threads never share sqlite handles.
        return None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def prune(self) -> int:
        if not self.enabled:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM query_audit WHERE created_at < ?", (cutoff,))
            return max(0, cursor.rowcount)

    def begin_query(
        self,
        *,
        request_id: str,
        username: str,
        question: str,
        provider: str,
        model: str,
        database_backend: str,
        schema_context_mode: str,
        thread_id: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        stored_question = redact_secrets(question) if self.store_question else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO query_audit (
                    request_id, thread_id, created_at, username, question, provider, model,
                    database_backend, schema_context_mode, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_progress')
                """,
                (
                    request_id,
                    thread_id,
                    _utc_now(),
                    username,
                    stored_question,
                    provider,
                    model,
                    database_backend,
                    schema_context_mode,
                ),
            )

    def finish_query(
        self,
        *,
        request_id: str,
        status: str,
        sql: str = "",
        retry_count: int = 0,
        error_types: list[str] | None = None,
        retrieved_tables: list[str] | None = None,
        result_row_count: int = 0,
        result_truncated: bool = False,
        latency_ms: int = 0,
        trace_id: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        model_calls: int = 0,
        confidence_score: float | None = None,
        confidence_level: str = "",
        cost_level: str = "",
        estimated_cost: float | None = None,
        cancelled: bool = False,
        timeline: list[dict[str, Any]] | None = None,
    ) -> None:
        if not self.enabled:
            return
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE query_audit SET
                    completed_at = ?, status = ?, sql_text = ?, retry_count = ?,
                    error_types_json = ?, retrieved_tables_json = ?,
                    result_row_count = ?, result_truncated = ?, latency_ms = ?,
                    trace_id = ?, prompt_tokens = ?, completion_tokens = ?, total_tokens = ?,
                    model_calls = ?, confidence_score = ?, confidence_level = ?,
                    cost_level = ?, estimated_cost = ?, cancelled = ?, timeline_json = ?
                WHERE request_id = ?
                """,
                (
                    _utc_now(),
                    status,
                    redact_secrets(sql),
                    max(0, retry_count),
                    json.dumps(error_types or [], ensure_ascii=False),
                    json.dumps(retrieved_tables or [], ensure_ascii=False),
                    max(0, result_row_count),
                    int(result_truncated),
                    max(0, latency_ms),
                    trace_id,
                    max(0, prompt_tokens), max(0, completion_tokens), max(0, total_tokens),
                    max(0, model_calls), confidence_score, confidence_level,
                    cost_level, estimated_cost, int(cancelled),
                    json.dumps(timeline or [], ensure_ascii=False),
                    request_id,
                ),
            )

    def add_feedback(self, request_id: str, username: str, verdict: str, comment: str = "") -> None:
        if not self.enabled:
            raise RuntimeError("audit logging is disabled")
        if verdict not in {"correct", "incorrect"}:
            raise ValueError("verdict must be correct or incorrect")
        now = _utc_now()
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM query_audit WHERE request_id = ?", (request_id,)
            ).fetchone()
            if not exists:
                raise KeyError(request_id)
            conn.execute(
                """
                INSERT INTO query_feedback (
                    request_id, username, verdict, comment, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id, username) DO UPDATE SET
                    verdict = excluded.verdict,
                    comment = excluded.comment,
                    updated_at = excluded.updated_at
                """,
                (request_id, username, verdict, redact_secrets(comment), now, now),
            )

    def list_queries(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        safe_limit = min(max(1, limit), 100)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT q.*, f.verdict AS feedback_verdict, f.comment AS feedback_comment,
                       f.updated_at AS feedback_updated_at
                FROM query_audit q
                LEFT JOIN query_feedback f
                  ON f.request_id = q.request_id AND f.username = q.username
                ORDER BY q.created_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [self._serialize(row) for row in rows]

    def record_resource_action(
        self, *, username: str, action: str, resource_type: str,
        resource_id: str | None = None, resource_name: str | None = None,
        status: str = "success", detail: str = "", request_id: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO resource_audit(
                   created_at, request_id, username, action, resource_type,
                   resource_id, resource_name, status, detail
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (_utc_now(), request_id, username, action, resource_type,
                 resource_id, redact_secrets(resource_name or ""), status,
                 redact_secrets(detail)[:500]),
            )

    def list_resource_actions(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        safe_limit = min(max(1, limit), 200)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM resource_audit ORDER BY created_at DESC LIMIT ?", (safe_limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_feedback(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT f.*, q.question, q.sql_text, q.status, q.model, q.confidence_score
                   FROM query_feedback f JOIN query_audit q ON q.request_id = f.request_id
                   ORDER BY f.updated_at DESC LIMIT ?""",
                (min(max(1, limit), 200),),
            ).fetchall()
        return [dict(row) for row in rows]

    def overview(self) -> dict[str, Any]:
        if not self.enabled:
            return {}
        with self._connect() as conn:
            query = conn.execute(
                """SELECT COUNT(*) total,
                          SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) successful,
                          SUM(CASE WHEN cancelled=1 THEN 1 ELSE 0 END) cancelled_count,
                          SUM(CASE WHEN confidence_level='low' THEN 1 ELSE 0 END) low_confidence,
                          COALESCE(AVG(latency_ms), 0) avg_latency_ms,
                          COALESCE(SUM(total_tokens), 0) total_tokens
                   FROM query_audit"""
            ).fetchone()
            feedback = conn.execute(
                """SELECT COUNT(*) total,
                          SUM(CASE WHEN verdict='correct' THEN 1 ELSE 0 END) correct,
                          SUM(CASE WHEN verdict='incorrect' THEN 1 ELSE 0 END) incorrect
                   FROM query_feedback"""
            ).fetchone()
            daily = conn.execute(
                """SELECT substr(created_at, 1, 10) day, COUNT(*) queries,
                          SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) successful
                   FROM query_audit GROUP BY substr(created_at, 1, 10)
                   ORDER BY day DESC LIMIT 14"""
            ).fetchall()
        q = dict(query)
        f = dict(feedback)
        total = int(q.get("total") or 0)
        rated = int(f.get("total") or 0)
        return {
            **q,
            "success_rate": round((q.get("successful") or 0) / total, 4) if total else None,
            "feedback_total": rated,
            "feedback_correct": int(f.get("correct") or 0),
            "feedback_incorrect": int(f.get("incorrect") or 0),
            "verified_accuracy": round((f.get("correct") or 0) / rated, 4) if rated else None,
            "daily": [dict(row) for row in reversed(daily)],
        }

    @staticmethod
    def _serialize(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["error_types"] = json.loads(result.pop("error_types_json"))
        result["retrieved_tables"] = json.loads(result.pop("retrieved_tables_json"))
        result["result_truncated"] = bool(result["result_truncated"])
        result["cancelled"] = bool(result.get("cancelled", 0))
        result["timeline"] = json.loads(result.pop("timeline_json", "[]") or "[]")
        return result
