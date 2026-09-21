from __future__ import annotations

import tempfile
import unittest
from fnmatch import fnmatch
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from Checkpoint import PlainRedisSaver, SanitizedCheckpointer, open_checkpointer
from Demo_db import build_demo_db
from Graph import build_graph
from Llm_client import MockLLMClient
from Retrieval import SQLiteSchemaRetriever


def _input(db_path: Path, question: str, history: list[dict] | None = None) -> dict:
    return {
        "question": question,
        "db_path": str(db_path),
        "retry_count": 0,
        "max_retries": 3,
        "error_history": [],
        "retrieval_events": [],
        "conversation_history": history or [],
        "schema_context": "",
        "retrieval_context": "",
        "current_sql": "",
        "generation_trace": {},
        "execution_rows": [],
        "execution_columns": [],
        "execution_error": "",
        "execution_error_type": "",
        "execution_truncated": False,
    }


class CheckpointTest(unittest.TestCase):
    def test_sqlite_persists_history_across_graph_recreation_and_sanitizes_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = build_demo_db(root / "demo.sqlite")
            settings = SimpleNamespace(
                checkpoint_backend="sqlite",
                checkpoint_sqlite_path="checkpoint.sqlite",
                checkpoint_ttl_minutes=60,
                redis_url="",
            )
            config = {"configurable": {"thread_id": "superadmin:test-thread"}}
            with open_checkpointer(settings, root) as saver:
                graph = build_graph(
                    llm_client=MockLLMClient(),
                    schema_retriever=SQLiteSchemaRetriever(top_tables=2),
                    initial_schema_rag=True,
                    checkpointer=saver,
                )
                result = graph.invoke(_input(db_path, "各部门的平均薪资是多少？"), config=config)
                self.assertTrue(result["execution_rows"])
                snapshot = graph.get_state(config)
                self.assertEqual(1, len(snapshot.values["conversation_history"]))
                self.assertFalse(snapshot.values.get("execution_rows"))
                self.assertFalse(snapshot.values.get("final_answer"))

            with open_checkpointer(settings, root) as saver:
                graph = build_graph(
                    llm_client=MockLLMClient(),
                    schema_retriever=SQLiteSchemaRetriever(top_tables=2),
                    initial_schema_rag=True,
                    checkpointer=saver,
                )
                history = list(graph.get_state(config).values["conversation_history"])
                result = graph.invoke(_input(db_path, "继续查询", history), config=config)
                self.assertEqual(2, len(result["conversation_history"]))

    def test_redis_backend_requires_url(self):
        settings = SimpleNamespace(
            checkpoint_backend="redis",
            checkpoint_sqlite_path="unused.sqlite",
            checkpoint_ttl_minutes=60,
            redis_url="",
        )
        with self.assertRaises(ValueError):
            with open_checkpointer(settings, Path(".")):
                pass

    def test_plain_redis_saver_runs_graph_without_search_modules(self):
        class FakeRedis:
            def __init__(self):
                self.data = {}
                self.expirations = {}

            def ping(self):
                return True

            def hset(self, key, mapping):
                target = self.data.setdefault(key, {})
                for field, value in mapping.items():
                    field = field.encode() if isinstance(field, str) else field
                    value = value.encode() if isinstance(value, str) else value
                    target[field] = value

            def hgetall(self, key):
                return dict(self.data.get(key, {}))

            def hvals(self, key):
                return list(self.data.get(key, {}).values())

            def expire(self, key, seconds):
                self.expirations[key] = seconds

            def scan_iter(self, match, count=100):
                return iter([key for key in self.data if fnmatch(key, match)])

            def delete(self, *keys):
                for key in keys:
                    self.data.pop(key, None)

            def close(self):
                return None

        fake = FakeRedis()
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "redis.Redis.from_url", return_value=fake
        ):
            db_path = build_demo_db(Path(temp_dir) / "demo.sqlite")
            saver = SanitizedCheckpointer(PlainRedisSaver("redis://test", ttl_minutes=2))
            graph = build_graph(llm_client=MockLLMClient(), checkpointer=saver)
            config = {"configurable": {"thread_id": "superadmin:redis-test"}}
            result = graph.invoke(_input(db_path, "列出所有员工姓名"), config=config)
            self.assertEqual("success", result["status"])
            self.assertEqual(1, len(graph.get_state(config).values["conversation_history"]))
            self.assertFalse(graph.get_state(config).values.get("execution_rows"))
            self.assertTrue(fake.expirations)
            saver.delete_thread("superadmin:redis-test")
            self.assertFalse(fake.data)


if __name__ == "__main__":
    unittest.main()
