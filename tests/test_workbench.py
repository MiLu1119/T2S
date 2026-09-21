from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from Workbench import WorkbenchStore
from Web_app import _csv_safe


class WorkbenchStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = WorkbenchStore(Path(self.temp_dir.name) / "workbench.sqlite", result_ttl_minutes=60)
        self.store.open()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _save(self, request_id: str, user_id: str = "user-1", thread_id: str = "thread-1"):
        self.store.save_query(
            request_id=request_id, user_id=user_id, thread_id=thread_id,
            data_source_id="source-1", question="各部门销售额 password=hidden",
            answer="查询完成", sql="SELECT department, amount FROM sales",
            status="success", columns=["department", "amount"],
            rows=[["华东", 100], ["华南", 80], ["华北", 60]],
            truncated=False, latency_ms=12,
        )

    def test_session_history_result_pagination_inputs_and_rename(self):
        self._save("request-1")
        sessions = self.store.list_sessions("user-1", "source-1")
        self.assertEqual(1, len(sessions))
        self.assertEqual(1, sessions[0]["query_count"])
        self.assertNotIn("hidden", sessions[0]["title"])
        renamed = self.store.rename_session("user-1", "thread-1", "source-1", "季度分析")
        self.assertEqual("季度分析", renamed["title"])
        queries = self.store.list_queries("user-1", "thread-1", "source-1")
        self.assertEqual("request-1", queries[0]["request_id"])
        result = self.store.get_result("user-1", "request-1")
        self.assertEqual(3, len(result["rows"]))
        self.assertEqual(["department", "amount"], result["columns"])

    def test_user_isolation_delete_and_csv_formula_protection(self):
        self._save("request-2")
        self.assertEqual([], self.store.list_sessions("user-2"))
        with self.assertRaises(KeyError):
            self.store.get_result("user-2", "request-2")
        self.assertEqual("'=cmd", _csv_safe("=cmd"))
        self.assertEqual(123, _csv_safe(123))
        self.assertTrue(self.store.delete_session("user-1", "thread-1", "source-1"))
        with self.assertRaises(KeyError):
            self.store.get_result("user-1", "request-2")


if __name__ == "__main__":
    unittest.main()
