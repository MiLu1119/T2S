from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from Demo_db import build_demo_db
from Sql_validator import validate_sql_ast


class SQLValidatorTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(build_demo_db(Path(self.temp_dir.name) / "demo.sqlite"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_valid_join_extracts_tables_and_columns(self):
        result = validate_sql_ast(
            "SELECT d.dept_name, AVG(e.salary) "
            "FROM departments d JOIN employees e ON d.dept_id=e.dept_id "
            "GROUP BY d.dept_name",
            self.db_path,
        )
        self.assertTrue(result.is_valid, result.reason)
        self.assertEqual(result.used_tables, ["departments", "employees"])
        self.assertIn("e.salary", result.used_columns)

    def test_unknown_table_has_suggestion(self):
        result = validate_sql_ast("SELECT name FROM employee", self.db_path)
        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_type, "unknown_table")
        self.assertIn("employees", result.suggestions)

    def test_unknown_qualified_column_has_suggestion(self):
        result = validate_sql_ast("SELECT e.emp_name FROM employees e", self.db_path)
        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_type, "unknown_column")
        self.assertIn("name", result.suggestions)

    def test_write_and_stacked_queries_are_rejected(self):
        write_result = validate_sql_ast("DELETE FROM employees", self.db_path)
        stacked_result = validate_sql_ast("SELECT 1; DROP TABLE employees", self.db_path)
        self.assertEqual(write_result.error_type, "unsafe_statement")
        self.assertEqual(stacked_result.error_type, "stacked_query")

    def test_keyword_inside_string_is_not_dangerous(self):
        result = validate_sql_ast("SELECT 'PRAGMA is only text' AS note", self.db_path)
        self.assertTrue(result.is_valid, result.reason)

    def test_cte_is_supported(self):
        result = validate_sql_ast(
            "WITH high_paid AS (SELECT name FROM employees WHERE salary > 100000) "
            "SELECT name FROM high_paid",
            self.db_path,
        )
        self.assertTrue(result.is_valid, result.reason)

    def test_postgres_qualified_table_uses_supplied_schema(self):
        result = validate_sql_ast(
            "SELECT o.amount FROM analytics.orders o",
            dialect="postgres",
            schema={"analytics.orders": {"order_id", "amount"}},
        )
        self.assertTrue(result.is_valid, result.reason)
        self.assertEqual(result.used_tables, ["analytics.orders"])

    def test_postgres_unqualified_ambiguous_table_is_rejected(self):
        result = validate_sql_ast(
            "SELECT amount FROM orders",
            dialect="postgres",
            schema={
                "analytics.orders": {"amount"},
                "archive.orders": {"amount"},
            },
        )
        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_type, "ambiguous_table")


if __name__ == "__main__":
    unittest.main()
