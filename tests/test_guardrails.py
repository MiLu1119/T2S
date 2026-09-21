from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from Cancellation import CancellationRegistry
from Database import SQLiteDatabase
from Executor import execute_sql
from Query_quality import assess_confidence
from Graph import build_graph


class QueryGuardrailTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "guardrails.sqlite"
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, category TEXT, amount REAL)")
            conn.executemany(
                "INSERT INTO events(category, amount) VALUES (?, ?)",
                [("a", float(index)) for index in range(20)],
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_sqlite_explain_detects_full_scan_without_executing_write(self):
        estimate = SQLiteDatabase(self.path).explain("SELECT * FROM events WHERE category='a'")
        self.assertTrue(estimate.available)
        self.assertGreaterEqual(estimate.full_scans, 1)
        self.assertIn(estimate.level, {"medium", "high"})

    def test_enforced_cost_threshold_blocks_execution_and_enters_handoff(self):
        class FixedLLM:
            last_trace = {}

            def generate_sql(self, **_kwargs):
                return "SELECT * FROM events"

        graph = build_graph(
            llm_client=FixedLLM(), database=SQLiteDatabase(self.path),
            max_retries=1, cost_enforce=True, cost_max_full_scans=0,
        )
        state = graph.invoke({
            "question": "全部事件", "db_path": str(self.path), "request_id": "cost-test",
            "retry_count": 0, "max_retries": 1, "error_history": [],
            "retrieval_events": [], "conversation_history": [], "schema_context": "",
            "retrieval_context": "", "generation_trace": {}, "execution_rows": [],
            "execution_columns": [], "execution_error": "", "execution_error_type": "",
            "execution_truncated": False, "cost_check": {}, "cancelled": False,
        })
        self.assertEqual("human_handoff", state["status"])
        self.assertFalse(state["cost_check"]["allowed"])
        self.assertEqual("cost", state["error_history"][0]["error_type"])

    def test_sqlite_long_query_can_be_cooperatively_cancelled(self):
        event = threading.Event()
        timer = threading.Timer(0.03, event.set)
        timer.start()
        result = execute_sql(
            str(self.path),
            "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt WHERE x<100000000) SELECT sum(x) FROM cnt",
            timeout_sec=3,
            cancel_event=event,
        )
        timer.cancel()
        self.assertFalse(result.success)
        self.assertEqual("cancelled", result.error_type)

    def test_cancellation_registry_enforces_request_ownership(self):
        registry = CancellationRegistry()
        event = registry.create("request-1", "user-1")
        with self.assertRaises(PermissionError):
            registry.cancel("request-1", "user-2")
        self.assertTrue(registry.cancel("request-1", "user-1"))
        self.assertTrue(event.is_set())

    def test_confidence_is_explainable_and_not_a_calibrated_probability(self):
        result = assess_confidence({
            "status": "success", "retry_count": 2, "execution_rows": [],
            "generation_trace": {"logprobs_available": False, "etc_triggered": True},
            "cost_check": {"level": "high"},
        })
        self.assertTrue(result["low_confidence"])
        self.assertFalse(result["calibrated_probability"])
        self.assertGreater(len(result["reasons"]), 2)


if __name__ == "__main__":
    unittest.main()
