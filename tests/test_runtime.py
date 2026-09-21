from __future__ import annotations

import tempfile
import unittest
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from Demo_db import build_demo_db
from Llm_client import VLLMClient
from Retrieval import SQLiteSchemaRetriever


class ETCTest(unittest.TestCase):
    def test_provider_token_usage_is_extracted(self):
        client = VLLMClient(enable_etc=False, check_server_ready=False)
        response = SimpleNamespace(
            choices=[SimpleNamespace(logprobs=None)],
            usage=SimpleNamespace(
                prompt_tokens=120,
                completion_tokens=30,
                total_tokens=150,
            ),
        )
        trace = client._build_generation_trace(response)
        self.assertEqual(
            {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
            trace["token_usage"],
        )

    def test_rising_entropy_triggers(self):
        client = VLLMClient(
            enable_etc=True,
            etc_first_diff_threshold=0.05,
            etc_second_diff_threshold=0.02,
            etc_window=2,
            check_server_ready=False,
        )
        self.assertIsNotNone(client._detect_etc_trigger([0.1, 0.11, 0.2, 0.4, 0.8]))

    def test_flat_entropy_does_not_trigger(self):
        client = VLLMClient(enable_etc=True, check_server_ready=False)
        self.assertIsNone(client._detect_etc_trigger([0.2, 0.2, 0.21, 0.2, 0.21, 0.2]))


class RetrievalTest(unittest.TestCase):
    def test_schema_and_values_are_retrieved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = build_demo_db(Path(temp_dir) / "demo.sqlite")
            result = SQLiteSchemaRetriever().retrieve(
                str(db_path), "查询员工的平均薪资", "SELECT AVG(salary) FROM"
            )
            self.assertIn("employees", result.tables)
            self.assertIn("salary", result.context)

    def test_zero_score_table_is_not_forced_into_top_k(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = build_demo_db(Path(temp_dir) / "demo.sqlite")
            result = SQLiteSchemaRetriever(top_tables=2).retrieve(
                str(db_path), "查询 sales 表的 amount"
            )
            self.assertEqual(["sales"], result.initial_tables)

    def test_fk_bridge_table_is_completed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bridge.sqlite"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE regions (region_id INTEGER PRIMARY KEY, region_name TEXT);
                CREATE TABLE customers (
                    customer_id INTEGER PRIMARY KEY,
                    region_id INTEGER REFERENCES regions(region_id)
                );
                CREATE TABLE orders (
                    order_id INTEGER PRIMARY KEY,
                    customer_id INTEGER REFERENCES customers(customer_id),
                    total REAL
                );
                """
            )
            conn.close()
            result = SQLiteSchemaRetriever(top_tables=2).retrieve(
                str(db_path), "orders regions"
            )
            self.assertEqual({"orders", "regions"}, set(result.initial_tables))
            self.assertEqual(["customers"], result.bridge_tables)
            self.assertTrue(
                any(
                    path in (
                        ["orders", "customers", "regions"],
                        ["regions", "customers", "orders"],
                    )
                    for path in result.foreign_key_paths
                )
            )

    def test_value_grounding_requires_explicit_allowlist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = build_demo_db(temp_path / "demo.sqlite")
            metadata_path = temp_path / "metadata.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "tables": {
                            "employees": {
                                "description": "员工薪资",
                                "value_grounding_columns": [],
                                "columns": {"name": "员工姓名", "salary": "工资"},
                            },
                            "departments": {
                                "description": "部门",
                                "value_grounding_columns": ["dept_name"],
                                "columns": {"dept_name": "部门名称"},
                            },
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = SQLiteSchemaRetriever(metadata_path=metadata_path, top_tables=2).retrieve(
                str(db_path), "员工所在部门名称"
            )
            self.assertIn("departments.dept_name", result.grounded_values)
            self.assertNotIn("employees.name", result.grounded_values)


if __name__ == "__main__":
    unittest.main()
