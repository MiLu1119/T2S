"""Local user sessions and encrypted data-source metadata for the Web product."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken


ROLE_PERMISSIONS: dict[str, set[str]] = {
    "superadmin": {"*"},
    "admin": {
        "users.read", "users.create", "users.update", "users.delete",
        "roles.read", "data_sources.create", "data_sources.read",
        "data_sources.update", "data_sources.delete", "data_sources.share",
        "query.execute", "sessions.manage", "feedback.create", "audit.read",
    },
    "developer": {
        "roles.read", "data_sources.create", "data_sources.read",
        "data_sources.update", "data_sources.delete", "data_sources.share",
        "query.execute", "sessions.manage", "feedback.create",
    },
    "analyst": {
        "roles.read", "data_sources.read", "query.execute",
        "sessions.manage", "feedback.create",
    },
    "viewer": {"roles.read", "data_sources.read"},
}

SOURCE_ACCESS_RANK = {"read": 1, "query": 2, "manage": 3}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def _password_valid(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_text, expected_text = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text)
        actual = _password_hash(password, salt).split("$", 2)[2]
        return hmac.compare_digest(actual, expected_text)
    except (ValueError, TypeError):
        return False


class IdentityStore:
    def __init__(
        self, path: str | Path, session_secret: str, credential_key: str,
        old_credential_keys: tuple[str, ...] = (),
    ):
        self.path = Path(path)
        if not session_secret:
            raise ValueError("SESSION_SECRET is required")
        if not credential_key:
            raise ValueError("DATA_SOURCE_MASTER_KEY is required")
        self.session_secret = hashlib.sha256(session_secret.encode()).digest()
        fernet_key = base64.urlsafe_b64encode(hashlib.sha256(credential_key.encode()).digest())
        self.cipher = Fernet(fernet_key)
        self.old_ciphers = [
            Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))
            for key in old_credential_keys if key and key != credential_key
        ]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def open(self, bootstrap_username: str, bootstrap_password: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS data_sources (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('sqlite', 'mysql', 'postgresql')),
                    config_json TEXT NOT NULL,
                    secret_ciphertext TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(owner_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash);
                CREATE INDEX IF NOT EXISTS idx_sources_owner ON data_sources(owner_id);
                CREATE TABLE IF NOT EXISTS roles (
                    name TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    system INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS role_permissions (
                    role_name TEXT NOT NULL REFERENCES roles(name) ON DELETE CASCADE,
                    permission TEXT NOT NULL,
                    PRIMARY KEY(role_name, permission)
                );
                CREATE TABLE IF NOT EXISTS user_roles (
                    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    role_name TEXT NOT NULL REFERENCES roles(name)
                );
                CREATE TABLE IF NOT EXISTS data_source_grants (
                    source_id TEXT NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    access_level TEXT NOT NULL CHECK(access_level IN ('read', 'query', 'manage')),
                    granted_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(source_id, user_id)
                );
                """
            )
            source_columns = {row[1] for row in conn.execute("PRAGMA table_info(data_sources)")}
            source_migrations = {
                "description": "TEXT NOT NULL DEFAULT ''",
                "health_status": "TEXT NOT NULL DEFAULT 'unknown'",
                "last_checked_at": "TEXT",
                "last_success_at": "TEXT",
                "last_error": "TEXT NOT NULL DEFAULT ''",
                "last_latency_ms": "INTEGER",
                "table_count": "INTEGER",
                "schema_synced_at": "TEXT",
            }
            for column, definition in source_migrations.items():
                if column not in source_columns:
                    conn.execute(f"ALTER TABLE data_sources ADD COLUMN {column} {definition}")
            descriptions = {
                "superadmin": "系统超级管理员", "admin": "管理员",
                "developer": "数据源开发者", "analyst": "查询分析员", "viewer": "只读访客",
            }
            for role_name, permissions in ROLE_PERMISSIONS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO roles(name, description, system) VALUES (?, ?, 1)",
                    (role_name, descriptions[role_name]),
                )
                conn.execute("DELETE FROM role_permissions WHERE role_name = ?", (role_name,))
                conn.executemany(
                    "INSERT OR IGNORE INTO role_permissions(role_name, permission) VALUES (?, ?)",
                    [(role_name, permission) for permission in permissions],
                )
            exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (bootstrap_username,)).fetchone()
            if not exists and bootstrap_password:
                conn.execute(
                    "INSERT INTO users(id, username, password_hash, role, created_at) VALUES (?, ?, ?, 'admin', ?)",
                    (str(uuid4()), bootstrap_username, _password_hash(bootstrap_password), _iso()),
                )
            # Existing installations used users.role directly. Preserve them while
            # introducing normalized role assignments; bootstrap owns superadmin.
            bootstrap = conn.execute("SELECT id FROM users WHERE username = ?", (bootstrap_username,)).fetchone()
            if bootstrap:
                conn.execute(
                    "INSERT OR REPLACE INTO user_roles(user_id, role_name) VALUES (?, 'superadmin')",
                    (bootstrap["id"],),
                )
            conn.execute(
                """INSERT OR IGNORE INTO user_roles(user_id, role_name)
                   SELECT id, CASE WHEN role = 'admin' THEN 'admin' ELSE 'analyst' END FROM users"""
            )
            self._migrate_postgresql_secrets(conn)
            self._rotate_credentials(conn)

    def _rotate_credentials(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT id, secret_ciphertext FROM data_sources WHERE secret_ciphertext != ''"
        ).fetchall()
        for row in rows:
            ciphertext = row["secret_ciphertext"].encode()
            try:
                self.cipher.decrypt(ciphertext)
                continue
            except InvalidToken:
                pass
            plaintext = None
            for old_cipher in self.old_ciphers:
                try:
                    plaintext = old_cipher.decrypt(ciphertext)
                    break
                except InvalidToken:
                    continue
            if plaintext is None:
                raise RuntimeError(
                    "data source credentials cannot be decrypted; configure DATA_SOURCE_OLD_MASTER_KEYS for rotation"
                )
            conn.execute(
                "UPDATE data_sources SET secret_ciphertext = ?, updated_at = ? WHERE id = ?",
                (self.cipher.encrypt(plaintext).decode(), _iso(), row["id"]),
            )

    def _migrate_postgresql_secrets(self, conn: sqlite3.Connection) -> None:
        """Move legacy PostgreSQL passwords out of config_json without logging them."""
        rows = conn.execute(
            "SELECT id, config_json, secret_ciphertext FROM data_sources WHERE kind = 'postgresql'"
        ).fetchall()
        for row in rows:
            config = json.loads(row["config_json"])
            dsn = str(config.get("dsn", ""))
            try:
                parsed = urlsplit(dsn)
            except ValueError:
                continue
            if parsed.password is None:
                continue
            host = parsed.hostname or ""
            if parsed.port:
                host = f"{host}:{parsed.port}"
            username = parsed.username or ""
            netloc = f"{username}@{host}" if username else host
            config["dsn"] = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
            encrypted = row["secret_ciphertext"] or self.cipher.encrypt(parsed.password.encode()).decode()
            conn.execute(
                "UPDATE data_sources SET config_json = ?, secret_ciphertext = ?, updated_at = ? WHERE id = ?",
                (json.dumps(config), encrypted, _iso(), row["id"]),
            )

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT u.id, u.username, u.password_hash, u.active,
                          COALESCE(ur.role_name, u.role) AS role
                   FROM users u LEFT JOIN user_roles ur ON ur.user_id = u.id
                   WHERE u.username = ?""",
                (username,),
            ).fetchone()
        if not row or not row["active"] or not _password_valid(password, row["password_hash"]):
            return None
        return {"id": row["id"], "username": row["username"], "role": row["role"]}

    def create_session(self, user_id: str, ttl_hours: int = 12) -> str:
        raw = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        expires = _now() + timedelta(hours=max(1, ttl_hours))
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(),))
            conn.execute(
                "INSERT INTO sessions(id, user_id, token_hash, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(uuid4()), user_id, token_hash, _iso(expires), _iso()),
            )
        signature = hmac.new(self.session_secret, raw.encode(), hashlib.sha256).hexdigest()
        return f"{raw}.{signature}"

    def resolve_session(self, token: str) -> dict[str, Any] | None:
        try:
            raw, signature = token.rsplit(".", 1)
        except ValueError:
            return None
        expected = hmac.new(self.session_secret, raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        with self._connect() as conn:
            row = conn.execute(
                """SELECT u.id, u.username, COALESCE(ur.role_name, u.role) AS role,
                          u.active, s.expires_at
                   FROM sessions s JOIN users u ON u.id = s.user_id
                   LEFT JOIN user_roles ur ON ur.user_id = u.id
                   WHERE s.token_hash = ?""",
                (hashlib.sha256(raw.encode()).hexdigest(),),
            ).fetchone()
        if not row or not row["active"] or datetime.fromisoformat(row["expires_at"]) <= _now():
            return None
        return {"id": row["id"], "username": row["username"], "role": row["role"]}

    def revoke_session(self, token: str) -> None:
        raw = token.rsplit(".", 1)[0]
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hashlib.sha256(raw.encode()).hexdigest(),))

    def create_user(self, username: str, password: str, role: str = "analyst") -> dict[str, Any]:
        role = "analyst" if role == "user" else role
        if role not in ROLE_PERMISSIONS:
            raise ValueError("unknown role")
        user_id = str(uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO users(id, username, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, username, _password_hash(password), "admin" if role in {"admin", "superadmin"} else "user", _iso()),
            )
            conn.execute("INSERT INTO user_roles(user_id, role_name) VALUES (?, ?)", (user_id, role))
        return {"id": user_id, "username": username, "role": role}

    def has_permission(self, user_id: str, permission: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT rp.permission FROM user_roles ur
                   JOIN role_permissions rp ON rp.role_name = ur.role_name
                   WHERE ur.user_id = ? AND rp.permission IN ('*', ?) LIMIT 1""",
                (user_id, permission),
            ).fetchone()
        return row is not None

    def list_roles(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT r.name, r.description, rp.permission FROM roles r
                   LEFT JOIN role_permissions rp ON rp.role_name = r.name
                   ORDER BY r.name, rp.permission"""
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = result.setdefault(row["name"], {"name": row["name"], "description": row["description"], "permissions": []})
            if row["permission"]:
                item["permissions"].append(row["permission"])
        return list(result.values())

    def list_users(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT u.id, u.username, u.active, u.created_at,
                          COALESCE(ur.role_name, u.role) AS role
                   FROM users u LEFT JOIN user_roles ur ON ur.user_id = u.id
                   ORDER BY u.created_at"""
            ).fetchall()
        return [dict(row) | {"active": bool(row["active"])} for row in rows]

    def get_user(self, user_id: str) -> dict[str, Any]:
        try:
            return next(user for user in self.list_users() if user["id"] == user_id)
        except StopIteration as exc:
            raise KeyError(user_id) from exc

    def update_user(self, user_id: str, *, role: str | None = None, active: bool | None = None) -> dict[str, Any]:
        if role is not None and role not in ROLE_PERMISSIONS:
            raise ValueError("unknown role")
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
                raise KeyError(user_id)
            if active is not None:
                conn.execute("UPDATE users SET active = ? WHERE id = ?", (int(active), user_id))
                if not active:
                    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            if role is not None:
                conn.execute("INSERT OR REPLACE INTO user_roles(user_id, role_name) VALUES (?, ?)", (user_id, role))
                conn.execute("UPDATE users SET role = ? WHERE id = ?", ("admin" if role in {"admin", "superadmin"} else "user", user_id))
        return self.get_user(user_id)

    def reset_password(self, user_id: str, password: str) -> None:
        with self._connect() as conn:
            cursor = conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (_password_hash(password), user_id))
            if not cursor.rowcount:
                raise KeyError(user_id)
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def delete_user(self, user_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cursor.rowcount > 0

    def add_source(
        self, owner_id: str, name: str, kind: str, config: dict[str, Any], secret: str = "",
        description: str = "", table_count: int | None = None,
    ) -> dict[str, Any]:
        source_id = str(uuid4())
        now = _iso()
        encrypted = self.cipher.encrypt(secret.encode()).decode() if secret else ""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO data_sources
                   (id, owner_id, name, kind, config_json, secret_ciphertext, description,
                    health_status, last_checked_at, last_success_at, table_count, schema_synced_at,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'healthy', ?, ?, ?, ?, ?, ?)""",
                (source_id, owner_id, name, kind, json.dumps(config), encrypted, description,
                 now, now, table_count, now, now, now),
            )
        return self.get_source(owner_id, source_id)

    def list_sources(self, owner_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT ds.*, owner.username AS owner_username,
                          CASE WHEN ds.owner_id = ? THEN 'manage' ELSE dsg.access_level END AS access_level,
                          ds.owner_id = ? AS is_owner
                   FROM data_sources ds
                   JOIN users owner ON owner.id = ds.owner_id
                   LEFT JOIN data_source_grants dsg ON dsg.source_id = ds.id AND dsg.user_id = ?
                   WHERE ds.owner_id = ? OR dsg.user_id = ? ORDER BY ds.created_at""",
                (owner_id, owner_id, owner_id, owner_id, owner_id),
            ).fetchall()
        return [self._public_source(row) for row in rows]

    def get_source(self, owner_id: str, source_id: str, include_secret: bool = False, required_access: str = "read") -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT ds.*, owner.username AS owner_username,
                          CASE WHEN ds.owner_id = ? THEN 'manage' ELSE dsg.access_level END AS access_level,
                          ds.owner_id = ? AS is_owner
                   FROM data_sources ds
                   JOIN users owner ON owner.id = ds.owner_id
                   LEFT JOIN data_source_grants dsg ON dsg.source_id = ds.id AND dsg.user_id = ?
                   WHERE ds.id = ? AND (ds.owner_id = ? OR dsg.user_id = ?)""",
                (owner_id, owner_id, owner_id, source_id, owner_id, owner_id),
            ).fetchone()
        if not row or SOURCE_ACCESS_RANK.get(row["access_level"], 0) < SOURCE_ACCESS_RANK[required_access]:
            raise KeyError(source_id)
        result = self._public_source(row)
        if include_secret:
            result["config"] = json.loads(row["config_json"])
        if include_secret and row["secret_ciphertext"]:
            try:
                result["secret"] = self.cipher.decrypt(row["secret_ciphertext"].encode()).decode()
            except InvalidToken as exc:
                raise RuntimeError("data source credential cannot be decrypted") from exc
        return result

    def delete_source(self, owner_id: str, source_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM data_sources WHERE id = ? AND owner_id = ?", (source_id, owner_id)
            )
        return cursor.rowcount > 0

    def update_source(
        self, actor_id: str, source_id: str, *, name: str | None = None,
        description: str | None = None, config: dict[str, Any] | None = None,
        secret: str | None = None,
    ) -> dict[str, Any]:
        current = self.get_source(actor_id, source_id, required_access="manage")
        if not current["is_owner"]:
            raise PermissionError("only the owner can edit connection settings")
        fields: list[str] = []
        values: list[Any] = []
        if name is not None:
            fields.append("name = ?")
            values.append(name)
        if description is not None:
            fields.append("description = ?")
            values.append(description)
        if config is not None:
            fields.append("config_json = ?")
            values.append(json.dumps(config))
        if secret is not None:
            fields.append("secret_ciphertext = ?")
            values.append(self.cipher.encrypt(secret.encode()).decode() if secret else "")
        fields.extend(["updated_at = ?", "health_status = 'unknown'", "last_error = ''"])
        values.extend([_iso(), source_id])
        with self._connect() as conn:
            conn.execute(f"UPDATE data_sources SET {', '.join(fields)} WHERE id = ?", values)
        return self.get_source(actor_id, source_id)

    def set_source_enabled(self, actor_id: str, source_id: str, enabled: bool) -> dict[str, Any]:
        current = self.get_source(actor_id, source_id, required_access="manage")
        if not current["is_owner"]:
            raise PermissionError("only the owner can enable or disable a data source")
        with self._connect() as conn:
            conn.execute(
                "UPDATE data_sources SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), _iso(), source_id),
            )
        return self.get_source(actor_id, source_id)

    def record_source_health(
        self, actor_id: str, source_id: str, *, healthy: bool, latency_ms: int,
        table_count: int | None = None, error: str = "",
    ) -> dict[str, Any]:
        self.get_source(actor_id, source_id, required_access="manage")
        now = _iso()
        safe_error = error[:300]
        with self._connect() as conn:
            conn.execute(
                """UPDATE data_sources SET health_status = ?, last_checked_at = ?,
                   last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,
                   last_error = ?, last_latency_ms = ?,
                   table_count = COALESCE(?, table_count),
                   schema_synced_at = CASE WHEN ? THEN ? ELSE schema_synced_at END,
                   updated_at = ? WHERE id = ?""",
                ("healthy" if healthy else "unhealthy", now, int(healthy), now,
                 "" if healthy else safe_error, max(0, latency_ms), table_count,
                 int(healthy), now, now, source_id),
            )
        return self.get_source(actor_id, source_id)

    def grant_source(self, actor_id: str, source_id: str, user_id: str, access_level: str) -> dict[str, Any]:
        if access_level not in SOURCE_ACCESS_RANK:
            raise ValueError("invalid access level")
        self.get_source(actor_id, source_id, required_access="manage")
        with self._connect() as conn:
            source = conn.execute("SELECT owner_id FROM data_sources WHERE id = ?", (source_id,)).fetchone()
            if not source or source["owner_id"] != actor_id:
                raise PermissionError("only the owner can share a data source")
            if source["owner_id"] == user_id:
                raise ValueError("owner does not need a grant")
            conn.execute(
                """INSERT INTO data_source_grants(source_id, user_id, access_level, granted_by, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(source_id, user_id) DO UPDATE SET access_level = excluded.access_level,
                   granted_by = excluded.granted_by""",
                (source_id, user_id, access_level, actor_id, _iso()),
            )
        return {"source_id": source_id, "user_id": user_id, "access_level": access_level}

    def list_source_grants(self, actor_id: str, source_id: str) -> list[dict[str, Any]]:
        self.get_source(actor_id, source_id, required_access="manage")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT dsg.user_id, u.username, dsg.access_level, dsg.created_at
                   FROM data_source_grants dsg JOIN users u ON u.id = dsg.user_id
                   WHERE dsg.source_id = ? ORDER BY u.username""", (source_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_source_grant(self, actor_id: str, source_id: str, user_id: str) -> bool:
        self.get_source(actor_id, source_id, required_access="manage")
        with self._connect() as conn:
            source = conn.execute("SELECT owner_id FROM data_sources WHERE id = ?", (source_id,)).fetchone()
            if not source or source["owner_id"] != actor_id:
                raise PermissionError("only the owner can revoke a grant")
            cursor = conn.execute("DELETE FROM data_source_grants WHERE source_id = ? AND user_id = ?", (source_id, user_id))
        return cursor.rowcount > 0

    @staticmethod
    def _public_source(row: sqlite3.Row) -> dict[str, Any]:
        config = json.loads(row["config_json"])
        if config.get("managed_upload"):
            config = {
                "managed_upload": True,
                "original_filename": config.get("original_filename", "database.sqlite"),
                "size_bytes": config.get("size_bytes", 0),
            }
        elif row["kind"] == "sqlite":
            config = {"registered_file": Path(config.get("path", "database.sqlite")).name}
        return {
            "id": row["id"], "name": row["name"], "kind": row["kind"],
            "config": config, "enabled": bool(row["enabled"]),
            "has_secret": bool(row["secret_ciphertext"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "access_level": row["access_level"] if "access_level" in row.keys() else "manage",
            "is_owner": bool(row["is_owner"]) if "is_owner" in row.keys() else True,
            "owner_id": row["owner_id"],
            "owner_username": row["owner_username"] if "owner_username" in row.keys() else "",
            "description": row["description"] if "description" in row.keys() else "",
            "health_status": row["health_status"] if "health_status" in row.keys() else "unknown",
            "last_checked_at": row["last_checked_at"] if "last_checked_at" in row.keys() else None,
            "last_success_at": row["last_success_at"] if "last_success_at" in row.keys() else None,
            "last_error": row["last_error"] if "last_error" in row.keys() else "",
            "last_latency_ms": row["last_latency_ms"] if "last_latency_ms" in row.keys() else None,
            "table_count": row["table_count"] if "table_count" in row.keys() else None,
            "schema_synced_at": row["schema_synced_at"] if "schema_synced_at" in row.keys() else None,
        }
