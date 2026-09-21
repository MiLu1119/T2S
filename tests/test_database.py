from __future__ import annotations

import unittest
from types import SimpleNamespace

from Database import PostgreSQLDatabase


class FakeCursor:
    def __init__(self, rows=None, columns=None):
        self._rows = list(rows or [])
        self.description = [SimpleNamespace(name=name) for name in (columns or [])]

    def fetchall(self):
        return list(self._rows)

    def fetchmany(self, size):
        return list(self._rows[:size])


class FakeConnection:
    def __init__(self):
        self.commands = []
        self.rollback_count = 0

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split()).lower()
        self.commands.append((normalized, params))
        if "information_schema.columns" in normalized:
            return FakeCursor(
                [
                    ("analytics", "orders", "order_id"),
                    ("analytics", "orders", "amount"),
                ]
            )
        if normalized.startswith("select set_config") or normalized.startswith("begin"):
            return FakeCursor()
        return FakeCursor([(1,), (2,), (3,)], ["value"])

    def rollback(self):
        self.rollback_count += 1


class ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self):
        self.connection_object = FakeConnection()
        self.opened = False
        self.closed = False

    def open(self, wait=True, timeout=None):
        self.opened = True

    def close(self):
        self.closed = True

    def connection(self, timeout=None):
        return ConnectionContext(self.connection_object)


class PostgreSQLDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.pool = FakePool()
        self.database = PostgreSQLDatabase(
            dsn="",
            schemas=("analytics",),
            pool=self.pool,
        )

    def test_pool_lifecycle_and_schema_loading(self):
        self.database.open()
        schema = self.database.get_schema()
        self.database.close()
        self.assertTrue(self.pool.opened)
        self.assertTrue(self.pool.closed)
        self.assertEqual(schema["analytics.orders"], {"order_id", "amount"})
        self.assertGreaterEqual(self.pool.connection_object.rollback_count, 1)

    def test_query_is_read_only_timed_rolled_back_and_truncated(self):
        result = self.database.execute("SELECT value FROM numbers", timeout_sec=2.5, max_rows=2)
        commands = self.pool.connection_object.commands
        self.assertTrue(result.success)
        self.assertEqual(result.rows, [(1,), (2,)])
        self.assertTrue(result.truncated)
        self.assertEqual(commands[0][0], "begin read only")
        self.assertIn("set_config('statement_timeout'", commands[1][0])
        self.assertEqual(commands[1][1], ("2500",))
        self.assertGreaterEqual(self.pool.connection_object.rollback_count, 1)


if __name__ == "__main__":
    unittest.main()
