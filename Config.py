"""Runtime configuration for the demo service and model adapters."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    provider: str = "mock"
    model: str = "qwen2.5-coder-7b"
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: str = "EMPTY"
    temperature: float = 0.0
    max_tokens: int = 1024
    request_timeout_sec: float = 60.0
    enable_etc: bool = False
    etc_first_diff_threshold: float = 0.12
    etc_second_diff_threshold: float = 0.08
    etc_window: int = 3
    max_retries: int = 3
    sql_timeout_sec: float = 5.0
    confidence_warning_threshold: float = 0.65
    sql_cost_enforce: bool = False
    sql_cost_max_estimated_cost: float = 100000.0
    sql_cost_max_estimated_rows: int = 1000000
    sql_cost_max_full_scans: int = 3
    web_username: str = "verisql"
    web_password: str = ""
    identity_db_path: str = "identity.sqlite"
    session_secret: str = ""
    session_ttl_hours: int = 12
    session_cookie_secure: bool = False
    data_source_master_key: str = ""
    data_source_old_master_keys: tuple[str, ...] = ()
    sqlite_allowed_roots: tuple[str, ...] = ()
    sqlite_upload_dir: str = "data/uploads"
    sqlite_upload_max_mb: int = 100
    database_backend: str = "sqlite"
    database_url: str = ""
    database_schemas: tuple[str, ...] = ("public",)
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    db_pool_timeout_sec: float = 5.0
    db_pool_max_lifetime_sec: float = 1800.0
    db_max_rows: int = 1000
    schema_context_mode: str = "full"
    schema_rag_top_tables: int = 3
    schema_rag_top_columns: int = 8
    schema_rag_value_samples: int = 5
    schema_catalog_db_path: str = "schema_catalog.sqlite"
    schema_embedding_enabled: bool = False
    schema_embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    schema_embedding_cache_dir: str = "models/fastembed"
    schema_embedding_weight: float = 0.45
    audit_enabled: bool = True
    audit_db_path: str = "audit.sqlite"
    audit_retention_days: int = 30
    audit_store_question: bool = True
    checkpoint_backend: str = "sqlite"
    checkpoint_sqlite_path: str = "checkpoints.sqlite"
    checkpoint_ttl_minutes: int = 1440
    session_history_turns: int = 5
    workbench_db_path: str = "workbench.sqlite"
    workbench_result_ttl_minutes: int = 1440
    redis_url: str = ""
    tracing_enabled: bool = False
    tracing_exporter: str = "none"
    otel_service_name: str = "verisql-agent"
    otel_environment: str = "development"
    otel_sample_ratio: float = 1.0

    @classmethod
    def from_env(cls) -> "Settings":
        provider = os.getenv("LLM_PROVIDER", "mock").strip().lower()
        if provider not in {"mock", "local", "external"}:
            raise ValueError("LLM_PROVIDER must be mock, local, or external")
        default_url = (
            "http://127.0.0.1:8000/v1"
            if provider != "external"
            else "https://api.openai.com/v1"
        )
        database_backend = os.getenv("DATABASE_BACKEND", "sqlite").strip().lower()
        if database_backend not in {"sqlite", "postgresql"}:
            raise ValueError("DATABASE_BACKEND must be sqlite or postgresql")
        schema_context_mode = os.getenv("SCHEMA_CONTEXT_MODE", "full").strip().lower()
        if schema_context_mode not in {"full", "rag"}:
            raise ValueError("SCHEMA_CONTEXT_MODE must be full or rag")
        checkpoint_backend = os.getenv("CHECKPOINT_BACKEND", "sqlite").strip().lower()
        if checkpoint_backend not in {"none", "sqlite", "redis", "redis-stack"}:
            raise ValueError("CHECKPOINT_BACKEND must be none, sqlite, redis, or redis-stack")
        tracing_exporter = os.getenv("TRACING_EXPORTER", "none").strip().lower()
        if tracing_exporter not in {"none", "console", "otlp"}:
            raise ValueError("TRACING_EXPORTER must be none, console, or otlp")
        otel_sample_ratio = float(os.getenv("OTEL_SAMPLE_RATIO", "1.0"))
        if not 0.0 <= otel_sample_ratio <= 1.0:
            raise ValueError("OTEL_SAMPLE_RATIO must be between 0 and 1")
        database_schemas = tuple(
            item.strip()
            for item in os.getenv("DATABASE_SCHEMAS", "public").split(",")
            if item.strip()
        )
        web_password = os.getenv("WEB_PASSWORD", "")
        session_secret = os.getenv("SESSION_SECRET", "") or web_password
        data_source_master_key = os.getenv("DATA_SOURCE_MASTER_KEY", "") or web_password
        data_source_old_master_keys = tuple(
            item.strip() for item in os.getenv("DATA_SOURCE_OLD_MASTER_KEYS", "").split(",") if item.strip()
        )
        sqlite_allowed_roots = tuple(
            item.strip() for item in os.getenv("SQLITE_ALLOWED_ROOTS", "").split(",") if item.strip()
        )
        return cls(
            provider=provider,
            model=os.getenv("LLM_MODEL", "qwen2.5-coder-7b"),
            base_url=os.getenv("LLM_BASE_URL", default_url).rstrip("/"),
            api_key=os.getenv("LLM_API_KEY", "EMPTY"),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "1024")),
            request_timeout_sec=float(os.getenv("LLM_REQUEST_TIMEOUT_SEC", "60")),
            enable_etc=_as_bool(os.getenv("LLM_ENABLE_ETC")),
            etc_first_diff_threshold=float(os.getenv("ETC_FIRST_DIFF_THRESHOLD", "0.12")),
            etc_second_diff_threshold=float(os.getenv("ETC_SECOND_DIFF_THRESHOLD", "0.08")),
            etc_window=int(os.getenv("ETC_WINDOW", "3")),
            max_retries=int(os.getenv("AGENT_MAX_RETRIES", "3")),
            sql_timeout_sec=float(os.getenv("SQL_TIMEOUT_SEC", "5")),
            confidence_warning_threshold=float(os.getenv("CONFIDENCE_WARNING_THRESHOLD", "0.65")),
            sql_cost_enforce=_as_bool(os.getenv("SQL_COST_ENFORCE"), False),
            sql_cost_max_estimated_cost=float(os.getenv("SQL_COST_MAX_ESTIMATED_COST", "100000")),
            sql_cost_max_estimated_rows=int(os.getenv("SQL_COST_MAX_ESTIMATED_ROWS", "1000000")),
            sql_cost_max_full_scans=int(os.getenv("SQL_COST_MAX_FULL_SCANS", "3")),
            web_username=os.getenv("WEB_USERNAME", "verisql"),
            web_password=web_password,
            identity_db_path=os.getenv("IDENTITY_DB_PATH", "identity.sqlite"),
            session_secret=session_secret,
            session_ttl_hours=max(1, int(os.getenv("SESSION_TTL_HOURS", "12"))),
            session_cookie_secure=_as_bool(os.getenv("SESSION_COOKIE_SECURE"), False),
            data_source_master_key=data_source_master_key,
            data_source_old_master_keys=data_source_old_master_keys,
            sqlite_allowed_roots=sqlite_allowed_roots,
            sqlite_upload_dir=os.getenv("SQLITE_UPLOAD_DIR", "data/uploads"),
            sqlite_upload_max_mb=max(1, int(os.getenv("SQLITE_UPLOAD_MAX_MB", "100"))),
            database_backend=database_backend,
            database_url=os.getenv("DATABASE_URL", ""),
            database_schemas=database_schemas or ("public",),
            db_pool_min_size=int(os.getenv("DB_POOL_MIN_SIZE", "1")),
            db_pool_max_size=int(os.getenv("DB_POOL_MAX_SIZE", "10")),
            db_pool_timeout_sec=float(os.getenv("DB_POOL_TIMEOUT_SEC", "5")),
            db_pool_max_lifetime_sec=float(os.getenv("DB_POOL_MAX_LIFETIME_SEC", "1800")),
            db_max_rows=int(os.getenv("DB_MAX_ROWS", "1000")),
            schema_context_mode=schema_context_mode,
            schema_rag_top_tables=int(os.getenv("SCHEMA_RAG_TOP_TABLES", "3")),
            schema_rag_top_columns=int(os.getenv("SCHEMA_RAG_TOP_COLUMNS", "8")),
            schema_rag_value_samples=int(os.getenv("SCHEMA_RAG_VALUE_SAMPLES", "5")),
            schema_catalog_db_path=os.getenv("SCHEMA_CATALOG_DB_PATH", "schema_catalog.sqlite"),
            schema_embedding_enabled=_as_bool(os.getenv("SCHEMA_EMBEDDING_ENABLED"), False),
            schema_embedding_model=os.getenv("SCHEMA_EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"),
            schema_embedding_cache_dir=os.getenv("SCHEMA_EMBEDDING_CACHE_DIR", "models/fastembed"),
            schema_embedding_weight=float(os.getenv("SCHEMA_EMBEDDING_WEIGHT", "0.45")),
            audit_enabled=_as_bool(os.getenv("AUDIT_ENABLED"), True),
            audit_db_path=os.getenv("AUDIT_DB_PATH", "audit.sqlite"),
            audit_retention_days=int(os.getenv("AUDIT_RETENTION_DAYS", "30")),
            audit_store_question=_as_bool(os.getenv("AUDIT_STORE_QUESTION"), True),
            checkpoint_backend=checkpoint_backend,
            checkpoint_sqlite_path=os.getenv("CHECKPOINT_SQLITE_PATH", "checkpoints.sqlite"),
            checkpoint_ttl_minutes=int(os.getenv("CHECKPOINT_TTL_MINUTES", "1440")),
            session_history_turns=max(1, int(os.getenv("SESSION_HISTORY_TURNS", "5"))),
            workbench_db_path=os.getenv("WORKBENCH_DB_PATH", "workbench.sqlite"),
            workbench_result_ttl_minutes=max(5, int(os.getenv("WORKBENCH_RESULT_TTL_MINUTES", "1440"))),
            redis_url=os.getenv("REDIS_URL", ""),
            tracing_enabled=_as_bool(os.getenv("TRACING_ENABLED"), False),
            tracing_exporter=tracing_exporter,
            otel_service_name=os.getenv("OTEL_SERVICE_NAME", "verisql-agent"),
            otel_environment=os.getenv("OTEL_ENVIRONMENT", "development"),
            otel_sample_ratio=otel_sample_ratio,
        )

    def public_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url if self.provider == "local" else None,
            "etc_enabled": self.enable_etc,
            "max_retries": self.max_retries,
            "database_backend": self.database_backend,
            "schema_context_mode": self.schema_context_mode,
            "schema_embedding_enabled": self.schema_embedding_enabled,
            "schema_embedding_model": self.schema_embedding_model if self.schema_embedding_enabled else None,
            "audit_enabled": self.audit_enabled,
            "checkpoint_backend": self.checkpoint_backend,
            "tracing_enabled": self.tracing_enabled,
            "tracing_exporter": self.tracing_exporter,
            "confidence_warning_threshold": self.confidence_warning_threshold,
            "sql_cost_enforce": self.sql_cost_enforce,
        }
