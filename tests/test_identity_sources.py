from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from Config import Settings
from Data_sources import inspect_uploaded_sqlite, normalize_source, validate_sqlite_path
from Identity import IdentityStore


class IdentityStoreTest(unittest.TestCase):
    def test_login_session_user_isolation_and_encrypted_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "identity.sqlite"
            store = IdentityStore(path, "session-secret", "credential-master-key")
            store.open("admin", "admin-password")
            admin = store.authenticate("admin", "admin-password")
            self.assertIsNotNone(admin)
            self.assertIsNone(store.authenticate("admin", "wrong"))
            token = store.create_session(admin["id"])
            self.assertEqual("admin", store.resolve_session(token)["username"])
            other = store.create_user("analyst", "long-password", "user")
            source = store.add_source(
                admin["id"], "mysql-prod", "mysql",
                {"host": "db.internal", "port": 3306, "database": "sales", "user": "reader"},
                "database-password",
            )
            self.assertTrue(source["has_secret"])
            self.assertNotIn("secret", source)
            self.assertEqual([], store.list_sources(other["id"]))
            self.assertEqual("database-password", store.get_source(admin["id"], source["id"], True)["secret"])
            raw = sqlite3.connect(path).execute("SELECT secret_ciphertext FROM data_sources").fetchone()[0]
            self.assertNotIn("database-password", raw)

    def test_rbac_user_lifecycle_and_source_grants(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = IdentityStore(Path(temp_dir) / "identity.sqlite", "session-secret", "master-key")
            store.open("root", "root-password")
            root = store.authenticate("root", "root-password")
            developer = store.create_user("developer", "developer-password", "developer")
            analyst = store.create_user("analyst", "analyst-password", "analyst")
            viewer = store.create_user("viewer", "viewer-password", "viewer")

            self.assertEqual("superadmin", root["role"])
            self.assertTrue(store.has_permission(root["id"], "audit.read"))
            self.assertTrue(store.has_permission(developer["id"], "data_sources.create"))
            self.assertFalse(store.has_permission(analyst["id"], "data_sources.create"))
            self.assertFalse(store.has_permission(viewer["id"], "query.execute"))

            source = store.add_source(developer["id"], "sales", "mysql", {
                "host": "db.internal", "port": 3306, "database": "sales", "user": "readonly"
            }, "secret")
            self.assertEqual([], store.list_sources(analyst["id"]))
            store.grant_source(developer["id"], source["id"], analyst["id"], "query")
            shared = store.get_source(analyst["id"], source["id"], include_secret=True, required_access="query")
            self.assertEqual("query", shared["access_level"])
            self.assertEqual("secret", shared["secret"])
            with self.assertRaises(KeyError):
                store.get_source(analyst["id"], source["id"], required_access="manage")
            with self.assertRaises(KeyError):
                store.grant_source(analyst["id"], source["id"], viewer["id"], "read")

            token = store.create_session(analyst["id"])
            store.update_user(analyst["id"], active=False)
            self.assertIsNone(store.resolve_session(token))
            store.update_user(analyst["id"], active=True, role="viewer")
            self.assertEqual("viewer", store.authenticate("analyst", "analyst-password")["role"])

            updated = store.update_source(
                developer["id"], source["id"], name="sales-v2",
                description="生产只读库", secret="rotated-secret",
            )
            self.assertEqual("sales-v2", updated["name"])
            self.assertEqual("生产只读库", updated["description"])
            self.assertEqual("rotated-secret", store.get_source(developer["id"], source["id"], True)["secret"])
            health = store.record_source_health(
                developer["id"], source["id"], healthy=True, latency_ms=12, table_count=7
            )
            self.assertEqual("healthy", health["health_status"])
            self.assertEqual(7, health["table_count"])
            disabled = store.set_source_enabled(developer["id"], source["id"], False)
            self.assertFalse(disabled["enabled"])

    def test_legacy_postgresql_password_is_migrated_out_of_plaintext_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "identity.sqlite"
            store = IdentityStore(path, "session-secret", "master-key")
            store.open("root", "root-password")
            root = store.authenticate("root", "root-password")
            source = store.add_source(
                root["id"], "postgres", "postgresql",
                {"dsn": "postgresql://reader@db.internal/app", "schemas": ["public"]},
            )
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "UPDATE data_sources SET config_json = ? WHERE id = ?",
                    ('{"dsn":"postgresql://reader:plain-secret@db.internal/app","schemas":["public"]}', source["id"]),
                )
            store.open("root", "root-password")
            migrated = store.get_source(root["id"], source["id"], include_secret=True)
            self.assertNotIn("plain-secret", migrated["config"]["dsn"])
            self.assertEqual("plain-secret", migrated["secret"])
            raw = sqlite3.connect(path).execute(
                "SELECT config_json, secret_ciphertext FROM data_sources WHERE id = ?", (source["id"],)
            ).fetchone()
            self.assertNotIn("plain-secret", raw[0])
            self.assertNotIn("plain-secret", raw[1])

    def test_data_source_master_key_rotation_reencrypts_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "identity.sqlite"
            old_store = IdentityStore(path, "session-secret", "old-master-key")
            old_store.open("root", "root-password")
            root = old_store.authenticate("root", "root-password")
            source = old_store.add_source(
                root["id"], "mysql", "mysql",
                {"host": "db.internal", "port": 3306, "database": "app", "user": "reader"},
                "database-password",
            )
            old_ciphertext = sqlite3.connect(path).execute(
                "SELECT secret_ciphertext FROM data_sources WHERE id = ?", (source["id"],)
            ).fetchone()[0]

            with self.assertRaisesRegex(RuntimeError, "OLD_MASTER_KEYS"):
                IdentityStore(path, "session-secret", "new-master-key").open("root", "root-password")

            rotated = IdentityStore(
                path, "session-secret", "new-master-key", ("old-master-key",)
            )
            rotated.open("root", "root-password")
            root = rotated.authenticate("root", "root-password")
            self.assertEqual(
                "database-password", rotated.get_source(root["id"], source["id"], include_secret=True)["secret"]
            )
            new_ciphertext = sqlite3.connect(path).execute(
                "SELECT secret_ciphertext FROM data_sources WHERE id = ?", (source["id"],)
            ).fetchone()[0]
            self.assertNotEqual(old_ciphertext, new_ciphertext)

    def test_bootstrap_and_existing_users_are_migrated_idempotently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "identity.sqlite"
            store = IdentityStore(path, "session-secret", "master-key")
            store.open("root", "root-password")
            store.create_user("legacy", "legacy-password", "user")
            store.open("root", "root-password")
            self.assertEqual("superadmin", store.authenticate("root", "root-password")["role"])
            self.assertEqual("analyst", store.authenticate("legacy", "legacy-password")["role"])

    def test_tampered_session_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = IdentityStore(Path(temp_dir) / "identity.sqlite", "session-secret", "master-key")
            store.open("admin", "password")
            admin = store.authenticate("admin", "password")
            token = store.create_session(admin["id"])
            self.assertIsNone(store.resolve_session(token + "tampered"))

    def test_public_sqlite_source_does_not_expose_server_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = IdentityStore(Path(temp_dir) / "identity.sqlite", "session-secret", "master-key")
            store.open("admin", "password")
            admin = store.authenticate("admin", "password")
            source = store.add_source(
                admin["id"], "private-path", "sqlite", {"path": "/srv/private/customer.sqlite"}
            )
            self.assertNotIn("path", source["config"])
            self.assertEqual("customer.sqlite", source["config"]["registered_file"])


class DataSourceValidationTest(unittest.TestCase):
    def test_uploaded_sqlite_header_integrity_and_tables(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "upload.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE metrics(id INTEGER PRIMARY KEY, value REAL)")
            conn.commit()
            conn.close()
            result = inspect_uploaded_sqlite(path)
            self.assertEqual(1, result["table_count"])
            self.assertEqual(["metrics"], result["tables"])

    def test_non_sqlite_upload_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "fake.db"
            path.write_bytes(b"not a sqlite database")
            with self.assertRaisesRegex(ValueError, "不是有效"):
                inspect_uploaded_sqlite(path)

    def test_sqlite_must_stay_inside_allowlisted_root(self):
        with tempfile.TemporaryDirectory() as allowed, tempfile.TemporaryDirectory() as denied:
            inside = Path(allowed) / "inside.sqlite"
            outside = Path(denied) / "outside.sqlite"
            sqlite3.connect(inside).close()
            sqlite3.connect(outside).close()
            self.assertEqual(inside.resolve(), validate_sqlite_path(str(inside), (allowed,), Path(allowed)))
            with self.assertRaisesRegex(ValueError, "outside"):
                validate_sqlite_path(str(outside), (allowed,), Path(allowed))

    def test_mysql_configuration_is_normalized_without_password(self):
        result = normalize_source(
            "mysql",
            {"host": "db.internal", "port": "3306", "database": "sales", "user": "reader", "ssl": True},
            Settings(),
            Path.cwd(),
        )
        self.assertEqual(3306, result["port"])
        self.assertNotIn("password", result)

    def test_postgresql_password_must_use_separate_secret_field(self):
        with self.assertRaisesRegex(ValueError, "separate password"):
            normalize_source(
                "postgresql", {"dsn": "postgresql://reader:secret@db.internal/app"},
                Settings(), Path.cwd(),
            )
        result = normalize_source(
            "postgresql", {"dsn": "postgresql://reader@db.internal/app", "schemas": ["public"]},
            Settings(), Path.cwd(),
        )
        self.assertNotIn("secret", result["dsn"])


if __name__ == "__main__":
    unittest.main()
