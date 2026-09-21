import sqlite3
import tempfile
import unittest
from pathlib import Path
import numpy as np

from Retrieval import SQLiteSchemaRetriever
from Schema_catalog import SchemaCatalogStore


class SchemaCatalogTest(unittest.TestCase):
    def test_catalog_is_source_scoped_persistent_and_infers_relationship(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "business.sqlite"
            with sqlite3.connect(db) as conn:
                conn.executescript(
                    """CREATE TABLE systems(id INTEGER PRIMARY KEY, name TEXT);
                       CREATE TABLE system_configs(id INTEGER PRIMARY KEY, system_id INTEGER,
                                                   config_key TEXT, config_value TEXT);"""
                )
            store = SchemaCatalogStore(root / "catalog.sqlite")
            store.open()
            catalog = store.sync_sqlite("source-a", db)
            self.assertEqual(catalog["source_id"], "source-a")
            self.assertIn("系统", catalog["tables"]["system_configs"]["description"])
            self.assertTrue(any(item["source_column"] == "system_id" for item in catalog["inferred_relations"]))
            self.assertEqual(store.get("source-a")["schema_hash"], catalog["schema_hash"])
            self.assertIsNone(store.get("source-b"))
            updated = store.apply_descriptions(
                "source-a", {"system_configs": {"description": "系统运行参数",
                "columns": {"config_key": "配置键", "invented": "不得新增"}}}, source="manual"
            )
            self.assertEqual("系统运行参数", updated["tables"]["system_configs"]["description"])
            self.assertNotIn("invented", updated["tables"]["system_configs"]["columns"])
            payload = store.model_payload(updated, ["system_configs"])
            self.assertEqual(["system_configs"], [item["name"] for item in payload])

    def test_catalog_metadata_improves_chinese_retrieval_and_weak_query_expands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "business.sqlite"
            with sqlite3.connect(db) as conn:
                for index in range(8):
                    conn.execute(f"CREATE TABLE misc_{index}(id INTEGER PRIMARY KEY, value TEXT)")
                conn.execute("CREATE TABLE system_configs(id INTEGER PRIMARY KEY, config_key TEXT)")
            store = SchemaCatalogStore(root / "catalog.sqlite")
            store.open()
            catalog = store.sync_sqlite("source-a", db)
            retriever = SQLiteSchemaRetriever(semantic_catalog=catalog, top_tables=2)
            result = retriever.retrieve(str(db), "系统配置一共有多少项")
            self.assertIn("system_configs", result.initial_tables)
            weak = retriever.retrieve(str(db), "完全无法匹配的业务词")
            self.assertGreaterEqual(len(weak.initial_tables), 6)

    def test_dense_semantics_can_recall_without_keyword_overlap(self):
        class FakeEmbedder:
            def embed(self, texts):
                vectors = []
                for text in texts:
                    semantic_user = any(term in text for term in ("登录账号", "sys_users", "系统用户"))
                    vectors.append([1.0, 0.0] if semantic_user else [0.0, 1.0])
                return np.asarray(vectors, dtype=np.float32)

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "business.sqlite"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE sys_users(id INTEGER PRIMARY KEY, login_name TEXT)")
                conn.execute("CREATE TABLE audit_events(id INTEGER PRIMARY KEY, payload TEXT)")
            retriever = SQLiteSchemaRetriever(top_tables=1, embedder=FakeEmbedder(), embedding_weight=.8)
            result = retriever.retrieve(str(db), "有哪些登录账号？")
            self.assertEqual(["sys_users"], result.initial_tables)


if __name__ == "__main__":
    unittest.main()
