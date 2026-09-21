"""Validation and runtime construction for user-managed data sources."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from Config import Settings
from Database import MySQLDatabase, PostgreSQLDatabase, SQLiteDatabase


SQLITE_HEADER = b"SQLite format 3\x00"


def inspect_uploaded_sqlite(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        if handle.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            raise ValueError("上传文件不是有效的 SQLite 3 数据库")
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        integrity = conn.execute("PRAGMA quick_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise ValueError("SQLite 完整性检查失败")
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        if not tables:
            raise ValueError("SQLite 数据库中没有可查询的表或视图")
        return {"table_count": len(tables), "tables": [row[0] for row in tables]}
    except sqlite3.DatabaseError as exc:
        raise ValueError("SQLite 数据库损坏或格式不受支持") from exc
    finally:
        if "conn" in locals():
            conn.close()


def validate_sqlite_path(raw_path: str, allowed_roots: tuple[str, ...], project_root: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    candidate = candidate.resolve(strict=True)
    roots = tuple(Path(item).expanduser().resolve() for item in allowed_roots) or (project_root.resolve(),)
    if not any(candidate == root or candidate.is_relative_to(root) for root in roots):
        raise ValueError("SQLite path is outside SQLITE_ALLOWED_ROOTS")
    if not candidate.is_file():
        raise ValueError("SQLite path must point to a database file")
    return candidate


def normalize_source(kind: str, config: dict[str, Any], settings: Settings, project_root: Path) -> dict[str, Any]:
    if kind == "sqlite":
        return {"path": str(validate_sqlite_path(str(config.get("path", "")), settings.sqlite_allowed_roots, project_root))}
    if kind == "mysql":
        host = str(config.get("host", "")).strip()
        database = str(config.get("database", "")).strip()
        user = str(config.get("user", "")).strip()
        port = int(config.get("port", 3306))
        if not host or not database or not user or not 1 <= port <= 65535:
            raise ValueError("MySQL host, port, database and user are required")
        return {
            "host": host, "port": port, "database": database, "user": user,
            "ssl": bool(config.get("ssl", False)),
        }
    if kind == "postgresql":
        dsn = str(config.get("dsn", "")).strip()
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("PostgreSQL DSN is invalid")
        parsed = urlsplit(dsn)
        if parsed.password is not None:
            raise ValueError("PostgreSQL password must be supplied in the separate password field")
        if not parsed.hostname or not parsed.path.strip("/"):
            raise ValueError("PostgreSQL host and database are required")
        schemas = [str(item).strip() for item in config.get("schemas", ["public"]) if str(item).strip()]
        return {"dsn": dsn, "schemas": schemas or ["public"]}
    raise ValueError("kind must be sqlite, mysql or postgresql")


def build_source_database(source: dict[str, Any], settings: Settings):
    kind, config, secret = source["kind"], source["config"], source.get("secret", "")
    if kind == "sqlite":
        return SQLiteDatabase(config["path"])
    if kind == "mysql":
        return MySQLDatabase(
            host=config["host"], port=config["port"], database=config["database"],
            user=config["user"], password=secret,
            max_size=settings.db_pool_max_size,
            connect_timeout_sec=settings.db_pool_timeout_sec,
            ssl=config.get("ssl", False),
        )
    dsn = config["dsn"]
    if secret:
        parsed = urlsplit(dsn)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        username = quote(parsed.username or "", safe="")
        netloc = f"{username}:{quote(secret, safe='')}@{host}" if username else host
        dsn = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    return PostgreSQLDatabase(
        dsn=dsn, schemas=tuple(config.get("schemas", ["public"])),
        min_size=settings.db_pool_min_size, max_size=settings.db_pool_max_size,
        pool_timeout_sec=settings.db_pool_timeout_sec,
        max_lifetime_sec=settings.db_pool_max_lifetime_sec,
    )
