from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from Audit import AuditStore, redact_secrets


class AuditStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = AuditStore(Path(self.temp_dir.name) / "audit.sqlite")
        self.store.open()

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def test_query_lifecycle_records_metadata_but_not_result_rows(self):
        self.store.begin_query(
            request_id="00000000-0000-0000-0000-000000000001",
            username="superadmin",
            question="各部门平均工资",
            provider="mock",
            model="mock-model",
            database_backend="sqlite",
            schema_context_mode="rag",
        )
        self.store.finish_query(
            request_id="00000000-0000-0000-0000-000000000001",
            status="success",
            sql="SELECT AVG(salary) FROM employees",
            retrieved_tables=["employees"],
            result_row_count=1,
            latency_ms=12,
        )
        item = self.store.list_queries()[0]
        self.assertEqual("success", item["status"])
        self.assertEqual(["employees"], item["retrieved_tables"])
        self.assertEqual(1, item["result_row_count"])
        self.assertNotIn("result_rows", item)

    def test_operational_metrics_include_trace_confidence_cost_and_feedback_accuracy(self):
        request_id = "00000000-0000-0000-0000-000000000099"
        self.store.begin_query(
            request_id=request_id, username="admin", question="统计订单",
            provider="external", model="model", database_backend="sqlite",
            schema_context_mode="full",
        )
        self.store.finish_query(
            request_id=request_id, status="success", trace_id="abc123",
            total_tokens=42, model_calls=1, confidence_score=0.61,
            confidence_level="low", cost_level="medium", estimated_cost=12.5,
            timeline=[{"node": "check_cost", "duration_ms": 2}],
        )
        self.store.add_feedback(request_id, "admin", "correct", "verified")
        item = self.store.list_queries()[0]
        overview = self.store.overview()
        self.assertEqual("abc123", item["trace_id"])
        self.assertEqual("check_cost", item["timeline"][0]["node"])
        self.assertEqual(42, overview["total_tokens"])
        self.assertEqual(1.0, overview["verified_accuracy"])
        self.assertEqual("correct", self.store.list_feedback()[0]["verdict"])

    def test_feedback_is_upserted_and_unknown_request_is_rejected(self):
        request_id = "00000000-0000-0000-0000-000000000002"
        self.store.begin_query(
            request_id=request_id,
            username="superadmin",
            question="查询员工",
            provider="mock",
            model="mock-model",
            database_backend="sqlite",
            schema_context_mode="rag",
        )
        self.store.add_feedback(request_id, "superadmin", "incorrect", "缺少排序")
        self.store.add_feedback(request_id, "superadmin", "correct", "已确认")
        item = self.store.list_queries()[0]
        self.assertEqual("correct", item["feedback_verdict"])
        self.assertEqual("已确认", item["feedback_comment"])
        with self.assertRaises(KeyError):
            self.store.add_feedback("missing", "superadmin", "correct")

    def test_question_and_feedback_secrets_are_redacted(self):
        secret = "ark-example-secret-value-123456"
        request_id = "00000000-0000-0000-0000-000000000003"
        self.store.begin_query(
            request_id=request_id,
            username="superadmin",
            question=f"api_key={secret}",
            provider="mock",
            model="mock-model",
            database_backend="sqlite",
            schema_context_mode="rag",
        )
        self.store.add_feedback(request_id, "superadmin", "incorrect", f"Bearer {secret}")
        item = self.store.list_queries()[0]
        self.assertNotIn(secret, item["question"])
        self.assertNotIn(secret, item["feedback_comment"])
        self.assertEqual("[REDACTED]", redact_secrets(f"password={secret}"))

    def test_question_storage_can_be_disabled(self):
        store = AuditStore(
            Path(self.temp_dir.name) / "private.sqlite",
            store_question=False,
        )
        store.open()
        store.begin_query(
            request_id="00000000-0000-0000-0000-000000000004",
            username="superadmin",
            question="不应保存的问题",
            provider="mock",
            model="mock-model",
            database_backend="sqlite",
            schema_context_mode="rag",
        )
        self.assertIsNone(store.list_queries()[0]["question"])

    def test_resource_actions_are_recorded_and_redacted(self):
        self.store.record_resource_action(
            username="superadmin", action="update", resource_type="data_source",
            resource_id="source-1", resource_name="production",
            detail="password=do-not-store",
        )
        item = self.store.list_resource_actions()[0]
        self.assertEqual("update", item["action"])
        self.assertEqual("source-1", item["resource_id"])
        self.assertNotIn("do-not-store", item["detail"])


if __name__ == "__main__":
    unittest.main()
