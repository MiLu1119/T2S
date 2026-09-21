"""FastAPI application for the interactive Text-to-SQL workbench."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import base64
import csv
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Any
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from Config import Settings
from Demo_db import DB_PATH, build_demo_db
from Graph import build_graph
from Retrieval import SQLiteSchemaRetriever
from Schema_catalog import SchemaCatalogStore
from Embedding import FastEmbedProvider
from Runtime import build_llm
from Database import build_database
from Audit import AuditStore
from Checkpoint import open_checkpointer
from Telemetry import configure_telemetry, set_span_attributes
from Identity import IdentityStore
from Data_sources import build_source_database, inspect_uploaded_sqlite, normalize_source
from Workbench import WorkbenchStore
from Cancellation import cancellations
from Query_quality import assess_confidence


BUNDLE_ROOT = Path(__file__).resolve().parent
ROOT = Path(os.getenv("VERISQL_RUNTIME_DIR", os.getcwd())).resolve()
WEB_DIR = BUNDLE_ROOT / "web"
logger = logging.getLogger(__name__)
tracer = trace.get_tracer("verisql.web")


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    data_source_id: str | None = None


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=256)


class UserCreateRequest(LoginRequest):
    password: str = Field(min_length=8, max_length=256)
    role: Literal["superadmin", "admin", "developer", "analyst", "viewer"] = "analyst"


class UserUpdateRequest(BaseModel):
    role: Literal["superadmin", "admin", "developer", "analyst", "viewer"] | None = None
    active: bool | None = None


class PasswordResetRequest(BaseModel):
    password: str = Field(min_length=8, max_length=256)


class SourceGrantRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    access_level: Literal["read", "query", "manage"] = "query"


class SessionRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class DataSourceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    kind: Literal["sqlite", "mysql", "postgresql"]
    config: dict[str, Any]
    password: str = Field(default="", max_length=512)


class DataSourceUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    config: dict[str, Any] | None = None
    password: str | None = Field(default=None, max_length=512)


class DataSourceEnabledRequest(BaseModel):
    enabled: bool


class QueryResponse(BaseModel):
    request_id: str
    thread_id: str
    status: str
    answer: str
    sql: str
    columns: list[str]
    rows: list[list[Any]]
    retry_count: int
    errors: list[dict[str, Any]]
    retrieval_events: list[dict[str, Any]]
    generation_trace: dict[str, Any]
    ast_validation: dict[str, Any]
    truncated: bool
    trace_id: str
    latency_ms: int
    timeline: list[dict[str, Any]]
    checkpoint_hit: bool
    data_source_id: str
    confidence: dict[str, Any]
    cost_check: dict[str, Any]


class FeedbackRequest(BaseModel):
    request_id: str = Field(min_length=36, max_length=36)
    verdict: Literal["correct", "incorrect"]
    comment: str = Field(default="", max_length=1000)


class CatalogEnrichRequest(BaseModel):
    table_names: list[str] = Field(default_factory=list, max_length=100)


class CatalogDescriptionPatch(BaseModel):
    tables: dict[str, Any]


NODE_LABELS = {
    "get_schema_context": "正在检索数据库结构",
    "generate_sql": "正在生成 SQL",
    "dynamic_retrieve": "不确定性触发二次检索",
    "corrective_retrieve": "错误驱动的 Schema 补充检索",
    "validate_sql": "正在进行 AST 安全校验",
    "check_cost": "正在检查 SQL 执行成本",
    "execute_sql": "正在只读执行 SQL",
    "reflect": "正在分析执行结果",
    "generate_answer": "正在整理查询结果",
    "human_handoff": "已转入人工处理",
    "cancel_query": "查询已安全取消",
}


def encode_sse(event: str, data: dict[str, Any]) -> str:
    """Encode one SSE event without exposing Python repr formatting."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not DB_PATH.exists():
        build_demo_db()
    settings = Settings.from_env()
    app.state.settings = settings
    app.state.telemetry = configure_telemetry(settings)
    app.state.database = build_database(settings)
    app.state.database.open()
    audit_path = Path(settings.audit_db_path)
    if not audit_path.is_absolute():
        audit_path = ROOT / audit_path
    app.state.audit = AuditStore(
        audit_path,
        enabled=settings.audit_enabled,
        retention_days=settings.audit_retention_days,
        store_question=settings.audit_store_question,
    )
    app.state.audit.open()
    workbench_path = Path(settings.workbench_db_path)
    if not workbench_path.is_absolute():
        workbench_path = ROOT / workbench_path
    app.state.workbench = WorkbenchStore(
        workbench_path, result_ttl_minutes=settings.workbench_result_ttl_minutes
    )
    app.state.workbench.open()
    identity_path = Path(settings.identity_db_path)
    if not identity_path.is_absolute():
        identity_path = ROOT / identity_path
    app.state.identity = IdentityStore(
        identity_path,
        settings.session_secret,
        settings.data_source_master_key,
        settings.data_source_old_master_keys,
    )
    app.state.identity.open(settings.web_username, settings.web_password)
    catalog_path = Path(settings.schema_catalog_db_path)
    if not catalog_path.is_absolute():
        catalog_path = ROOT / catalog_path
    app.state.schema_catalogs = SchemaCatalogStore(catalog_path)
    app.state.schema_catalogs.open()
    app.state.schema_embedder = None
    if settings.schema_embedding_enabled:
        embedding_cache = Path(settings.schema_embedding_cache_dir)
        if not embedding_cache.is_absolute():
            embedding_cache = ROOT / embedding_cache
        app.state.schema_embedder = FastEmbedProvider(
            settings.schema_embedding_model, embedding_cache, threads=min(8, os.cpu_count() or 4)
        )
    bootstrap = app.state.identity.authenticate(settings.web_username, settings.web_password)
    if bootstrap and not app.state.identity.list_sources(bootstrap["id"]):
        app.state.identity.add_source(
            bootstrap["id"], "内置演示数据库", "sqlite", {"path": str(DB_PATH.resolve())}
        )
    app.state.source_runtimes = {}
    app.state.cancellations = cancellations
    app.state.checkpoint_context = open_checkpointer(settings, ROOT)
    app.state.checkpointer = app.state.checkpoint_context.__enter__()
    schema_retriever = None
    if settings.database_backend == "sqlite" and (
        settings.schema_context_mode == "rag" or settings.enable_etc
    ):
        schema_retriever = SQLiteSchemaRetriever(
            embedder=app.state.schema_embedder,
            embedding_weight=settings.schema_embedding_weight,
            top_tables=settings.schema_rag_top_tables,
            top_columns=settings.schema_rag_top_columns,
            max_value_samples=settings.schema_rag_value_samples,
        )
    app.state.effective_schema_context_mode = (
        "rag" if settings.schema_context_mode == "rag" and schema_retriever else "full"
    )
    app.state.graph = build_graph(
        llm_client=build_llm(settings),
        max_retries=settings.max_retries,
        execute_timeout_sec=settings.sql_timeout_sec,
        schema_retriever=schema_retriever,
        database=app.state.database,
        max_result_rows=settings.db_max_rows,
        initial_schema_rag=app.state.effective_schema_context_mode == "rag",
        cost_enforce=settings.sql_cost_enforce,
        cost_max_estimated_cost=settings.sql_cost_max_estimated_cost,
        cost_max_estimated_rows=settings.sql_cost_max_estimated_rows,
        cost_max_full_scans=settings.sql_cost_max_full_scans,
        checkpointer=app.state.checkpointer,
    )
    try:
        yield
    finally:
        for runtime in app.state.source_runtimes.values():
            runtime["database"].close()
        app.state.database.close()
        app.state.audit.close()
        app.state.workbench.close()
        app.state.checkpoint_context.__exit__(None, None, None)
        app.state.telemetry.shutdown()


app = FastAPI(title="VeriSQL Agent", version="1.0.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")
FastAPIInstrumentor.instrument_app(app, excluded_urls="/api/health")


@app.middleware("http")
async def basic_auth(request, call_next):
    settings = getattr(request.app.state, "settings", None)
    public_path = (
        request.url.path == "/"
        or request.url.path == "/api/health"
        or request.url.path == "/api/auth/login"
        or request.url.path.startswith("/assets/")
    )
    if settings and settings.web_password and not public_path:
        session_token = request.cookies.get("verisql_session", "")
        user = request.app.state.identity.resolve_session(session_token) if session_token else None
        authorization = request.headers.get("Authorization", "")
        if not user and authorization:
            try:
                scheme, encoded = authorization.split(" ", 1)
                username, password = base64.b64decode(encoded).decode("utf-8").split(":", 1)
                user = request.app.state.identity.authenticate(username, password) if scheme.lower() == "basic" else None
            except (ValueError, UnicodeDecodeError):
                user = None
        if not user:
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
            )
        request.state.user = user
        request.state.authenticated_user = user["username"]
    else:
        request.state.authenticated_user = "anonymous"
    return await call_next(request)


@app.post("/api/auth/login")
def login(payload: LoginRequest, response: Response):
    user = app.state.identity.authenticate(payload.username, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = app.state.identity.create_session(user["id"], app.state.settings.session_ttl_hours)
    response.set_cookie(
        "verisql_session", token, httponly=True, samesite="strict",
        secure=app.state.settings.session_cookie_secure,
        max_age=app.state.settings.session_ttl_hours * 3600,
    )
    return {"user": {"username": user["username"], "role": user["role"]}}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("verisql_session", "")
    if token:
        app.state.identity.revoke_session(token)
    response.delete_cookie("verisql_session")
    return {"status": "logged_out"}


@app.get("/api/auth/me")
def me(request: Request):
    user = request.state.user
    return {
        "id": user["id"], "username": user["username"], "role": user["role"],
        "permissions": [
            permission for permission in (
                "users.read", "users.create", "users.update", "users.delete",
                "roles.read", "data_sources.create", "data_sources.read",
                "data_sources.update", "data_sources.delete", "data_sources.share",
                "query.execute", "sessions.manage", "feedback.create", "audit.read",
            ) if app.state.identity.has_permission(user["id"], permission)
        ],
    }


def require_permission(request: Request, permission: str) -> dict[str, Any]:
    user = request.state.user
    if not app.state.identity.has_permission(user["id"], permission):
        raise HTTPException(status_code=403, detail=f"permission required: {permission}")
    return user


@app.post("/api/users")
def create_user(payload: UserCreateRequest, request: Request):
    require_permission(request, "users.create")
    if payload.role == "superadmin" and request.state.user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="only superadmin can assign superadmin")
    try:
        return app.state.identity.create_user(payload.username, payload.password, payload.role)
    except Exception as exc:
        raise HTTPException(status_code=409, detail="username already exists") from exc


@app.get("/api/users")
def list_users(request: Request):
    require_permission(request, "users.read")
    return {"items": app.state.identity.list_users()}


@app.patch("/api/users/{user_id}")
def update_user(user_id: str, payload: UserUpdateRequest, request: Request):
    require_permission(request, "users.update")
    try:
        target = app.state.identity.get_user(user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc
    if (target["role"] == "superadmin" or payload.role == "superadmin") and request.state.user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="only superadmin can manage superadmin")
    if user_id == request.state.user["id"] and payload.active is False:
        raise HTTPException(status_code=400, detail="不能停用当前登录用户")
    if user_id == request.state.user["id"] and payload.role and payload.role != request.state.user["role"]:
        raise HTTPException(status_code=400, detail="不能修改自己的角色")
    try:
        return app.state.identity.update_user(user_id, role=payload.role, active=payload.active)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc


@app.post("/api/users/{user_id}/reset-password")
def reset_user_password(user_id: str, payload: PasswordResetRequest, request: Request):
    require_permission(request, "users.update")
    try:
        target = app.state.identity.get_user(user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc
    if target["role"] == "superadmin" and request.state.user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="only superadmin can manage superadmin")
    try:
        app.state.identity.reset_password(user_id, payload.password)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc
    return {"status": "password_reset", "user_id": user_id}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: str, request: Request):
    require_permission(request, "users.delete")
    try:
        target = app.state.identity.get_user(user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc
    if target["role"] == "superadmin":
        raise HTTPException(status_code=403, detail="superadmin cannot be deleted")
    if user_id == request.state.user["id"]:
        raise HTTPException(status_code=400, detail="不能删除当前登录用户")
    try:
        deleted = app.state.identity.delete_user(user_id)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="用户仍拥有数据源，请先转移或删除资源") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="user not found")
    return {"status": "deleted", "user_id": user_id}


@app.get("/api/roles")
def list_roles(request: Request):
    require_permission(request, "roles.read")
    return {"items": app.state.identity.list_roles()}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/config")
def config():
    result = app.state.settings.public_dict()
    result["schema_context_mode"] = app.state.effective_schema_context_mode
    return result


@app.get("/api/data-sources")
def list_data_sources(request: Request):
    require_permission(request, "data_sources.read")
    return {"items": app.state.identity.list_sources(request.state.user["id"])}


def _build_and_test_source(kind: str, config_data: dict[str, Any], password: str):
    normalized = normalize_source(kind, config_data, app.state.settings, ROOT)
    source = {"kind": kind, "config": normalized, "secret": password}
    database = build_source_database(source, app.state.settings)
    database.open()
    try:
        schema = database.get_schema()
        if not schema:
            raise ValueError("数据库连接成功，但没有发现可访问的表")
        return normalized, len(schema)
    finally:
        database.close()


def _invalidate_source_runtime(source_id: str) -> None:
    for key in [key for key in app.state.source_runtimes if key[1] == source_id]:
        runtime = app.state.source_runtimes.pop(key)
        runtime["database"].close()


def _audit_source_action(
    request: Request, action: str, *, source_id: str | None = None,
    source_name: str = "", status: str = "success", detail: str = "",
) -> None:
    try:
        app.state.audit.record_resource_action(
            username=request.state.user["username"], action=action,
            resource_type="data_source", resource_id=source_id,
            resource_name=source_name, status=status, detail=detail,
        )
    except Exception:
        logger.exception("Failed to record data source audit action %s", action)


@app.post("/api/data-sources/test")
def test_data_source(payload: DataSourceRequest, request: Request):
    require_permission(request, "data_sources.create")
    try:
        _, table_count = _build_and_test_source(payload.kind, payload.config, payload.password)
    except Exception as exc:
        _audit_source_action(
            request, "connection_test", source_name=payload.name, status="failed",
            detail=f"connection failed ({type(exc).__name__})",
        )
        raise HTTPException(status_code=400, detail=f"连接测试失败（{type(exc).__name__}）") from exc
    _audit_source_action(request, "connection_test", source_name=payload.name)
    return {"status": "ok", "table_count": table_count}


@app.post("/api/data-sources", status_code=201)
def create_data_source(payload: DataSourceRequest, request: Request):
    require_permission(request, "data_sources.create")
    try:
        normalized, table_count = _build_and_test_source(payload.kind, payload.config, payload.password)
        source = app.state.identity.add_source(
            request.state.user["id"], payload.name.strip(), payload.kind, normalized, payload.password,
            description=payload.description.strip(), table_count=table_count,
        )
    except ValueError as exc:
        _audit_source_action(request, "create", source_name=payload.name, status="failed", detail="validation failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        _audit_source_action(request, "create", source_name=payload.name, status="failed", detail="name conflict")
        raise HTTPException(status_code=409, detail="数据源名称已经存在") from exc
    except Exception as exc:
        logger.info("Data source connection failed for user %s: %s", request.state.user["username"], type(exc).__name__)
        _audit_source_action(
            request, "create", source_name=payload.name, status="failed",
            detail=f"connection failed ({type(exc).__name__})",
        )
        raise HTTPException(status_code=400, detail="数据源连接测试失败") from exc
    source["table_count"] = table_count
    _audit_source_action(request, "create", source_id=source["id"], source_name=source["name"])
    return source


@app.post("/api/data-sources/upload-sqlite", status_code=201)
async def upload_sqlite_source(
    request: Request,
    name: str = Form(min_length=1, max_length=80),
    description: str = Form(default="", max_length=500),
    file: UploadFile = File(...),
):
    require_permission(request, "data_sources.create")
    original_name = Path(file.filename or "database.sqlite").name
    if Path(original_name).suffix.lower() not in {".sqlite", ".sqlite3", ".db"}:
        raise HTTPException(status_code=400, detail="只支持 .sqlite、.sqlite3 或 .db 文件")
    settings = app.state.settings
    upload_root = Path(settings.sqlite_upload_dir)
    if not upload_root.is_absolute():
        upload_root = ROOT / upload_root
    user_dir = upload_root.resolve() / request.state.user["id"]
    user_dir.mkdir(parents=True, exist_ok=True)
    stored_path = user_dir / f"{uuid4().hex}.sqlite"
    temporary_path = stored_path.with_suffix(".uploading")
    max_bytes = settings.sqlite_upload_max_mb * 1024 * 1024
    written = 0
    try:
        with temporary_path.open("xb") as output:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(f"文件不能超过 {settings.sqlite_upload_max_mb} MB")
                output.write(chunk)
        inspection = inspect_uploaded_sqlite(temporary_path)
        temporary_path.replace(stored_path)
        source = app.state.identity.add_source(
            request.state.user["id"], name.strip(), "sqlite",
            {
                "path": str(stored_path),
                "managed_upload": True,
                "original_filename": original_name,
                "size_bytes": written,
            },
            description=description.strip(),
            table_count=inspection["table_count"],
        )
        seed_metadata = {}
        seed_path = ROOT / "schema_metadata.json"
        if seed_path.exists():
            try:
                seed_metadata = json.loads(seed_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                seed_metadata = {}
        app.state.schema_catalogs.sync_sqlite(
            source["id"], stored_path, source_description=description.strip(),
            seed_metadata=seed_metadata,
        )
    except sqlite3.IntegrityError as exc:
        if stored_path.exists():
            stored_path.unlink()
        _audit_source_action(request, "create", source_name=name, status="failed", detail="name conflict")
        raise HTTPException(status_code=409, detail="数据源名称已经存在") from exc
    except ValueError as exc:
        if stored_path.exists():
            stored_path.unlink()
        _audit_source_action(request, "create", source_name=name, status="failed", detail="upload validation failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        if stored_path.exists():
            stored_path.unlink()
        logger.exception("SQLite upload failed for user %s", request.state.user["username"])
        _audit_source_action(
            request, "create", source_name=name, status="failed",
            detail=f"upload failed ({type(exc).__name__})",
        )
        raise HTTPException(status_code=500, detail="SQLite 上传处理失败") from exc
    finally:
        await file.close()
        if temporary_path.exists():
            temporary_path.unlink()
    source.update(inspection)
    _audit_source_action(request, "create", source_id=source["id"], source_name=source["name"])
    return source


@app.patch("/api/data-sources/{source_id}")
def update_data_source(source_id: str, payload: DataSourceUpdateRequest, request: Request):
    require_permission(request, "data_sources.update")
    try:
        current = app.state.identity.get_source(
            request.state.user["id"], source_id, include_secret=True, required_access="manage"
        )
        config = current["config"]
        secret = current.get("secret", "")
        table_count = current.get("table_count")
        if payload.config is not None or payload.password is not None:
            validation_started = time.perf_counter()
            candidate_config = payload.config if payload.config is not None else config
            candidate_secret = payload.password if payload.password is not None else secret
            config, table_count = _build_and_test_source(current["kind"], candidate_config, candidate_secret)
            secret = candidate_secret
        updated = app.state.identity.update_source(
            request.state.user["id"], source_id,
            name=payload.name.strip() if payload.name is not None else None,
            description=payload.description.strip() if payload.description is not None else None,
            config=config if payload.config is not None else None,
            secret=secret if payload.password is not None else None,
        )
        if payload.config is not None or payload.password is not None:
            updated = app.state.identity.record_source_health(
                request.state.user["id"], source_id, healthy=True,
                latency_ms=int((time.perf_counter() - validation_started) * 1000), table_count=table_count,
            )
        _invalidate_source_runtime(source_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="data source not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="数据源名称已经存在") from exc
    except Exception as exc:
        _audit_source_action(
            request, "update", source_id=source_id, status="failed",
            detail=f"update failed ({type(exc).__name__})",
        )
        raise HTTPException(status_code=400, detail=f"数据源更新失败：{type(exc).__name__}") from exc
    _audit_source_action(request, "update", source_id=source_id, source_name=updated["name"])
    return updated


@app.post("/api/data-sources/{source_id}/health")
def check_data_source_health(source_id: str, request: Request):
    require_permission(request, "data_sources.update")
    started_at = time.perf_counter()
    source_name = ""
    try:
        source = app.state.identity.get_source(
            request.state.user["id"], source_id, include_secret=True, required_access="manage"
        )
        source_name = source["name"]
        database = build_source_database(source, app.state.settings)
        database.open()
        try:
            table_count = len(database.get_schema())
        finally:
            database.close()
        result = app.state.identity.record_source_health(
            request.state.user["id"], source_id, healthy=True,
            latency_ms=int((time.perf_counter() - started_at) * 1000), table_count=table_count,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="data source not found") from exc
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        safe_error = f"连接失败（{type(exc).__name__}）"
        try:
            app.state.identity.record_source_health(
                request.state.user["id"], source_id, healthy=False,
                latency_ms=latency_ms, error=safe_error,
            )
        except Exception:
            pass
        _audit_source_action(
            request, "health_check", source_id=source_id, source_name=source_name,
            status="failed", detail=safe_error,
        )
        raise HTTPException(status_code=400, detail=safe_error) from exc
    _audit_source_action(request, "health_check", source_id=source_id, source_name=source_name)
    return result


@app.get("/api/data-sources/{source_id}/schema-catalog")
def get_schema_catalog(source_id: str, request: Request):
    require_permission(request, "data_sources.read")
    try:
        app.state.identity.get_source(request.state.user["id"], source_id, required_access="read")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="data source not found") from exc
    catalog = app.state.schema_catalogs.get(source_id)
    if catalog is None:
        raise HTTPException(status_code=404, detail="schema catalog has not been synchronized")
    return catalog


@app.post("/api/data-sources/{source_id}/schema-catalog/sync")
def sync_schema_catalog(source_id: str, request: Request):
    require_permission(request, "data_sources.update")
    try:
        source = app.state.identity.get_source(
            request.state.user["id"], source_id, include_secret=True, required_access="manage"
        )
        if source["kind"] != "sqlite":
            raise HTTPException(status_code=400, detail="当前 Catalog 自动同步仅支持 SQLite")
        seed_metadata = {}
        seed_path = ROOT / "schema_metadata.json"
        if seed_path.exists():
            seed_metadata = json.loads(seed_path.read_text(encoding="utf-8"))
        catalog = app.state.schema_catalogs.sync_sqlite(
            source_id, source["config"]["path"], source_description=source.get("description", ""),
            seed_metadata=seed_metadata,
        )
        _invalidate_source_runtime(source_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="data source not found") from exc
    _audit_source_action(request, "schema_catalog_sync", source_id=source_id, source_name=source["name"])
    return {"source_id": source_id, "schema_hash": catalog["schema_hash"],
            "table_count": len(catalog["tables"]),
            "inferred_relation_count": len(catalog["inferred_relations"]),
            "generated_at": catalog["generated_at"]}


@app.post("/api/data-sources/{source_id}/schema-catalog/enrich")
def enrich_schema_catalog(source_id: str, payload: CatalogEnrichRequest, request: Request):
    require_permission(request, "data_sources.update")
    try:
        source = app.state.identity.get_source(
            request.state.user["id"], source_id, required_access="manage"
        )
        catalog = app.state.schema_catalogs.get(source_id)
        if catalog is None:
            raise HTTPException(status_code=409, detail="请先同步 Schema Catalog")
        unknown = sorted(set(payload.table_names) - set(catalog["tables"]))
        if unknown:
            raise HTTPException(status_code=400, detail=f"未知表：{', '.join(unknown[:5])}")
        model_input = app.state.schema_catalogs.model_payload(
            catalog, payload.table_names or None
        )
        llm = build_llm(app.state.settings)
        updates: dict[str, Any] = {}
        for offset in range(0, len(model_input), 8):
            updates.update(llm.describe_schema(model_input[offset:offset + 8]))
        catalog = app.state.schema_catalogs.apply_descriptions(source_id, updates, source="llm")
        _invalidate_source_runtime(source_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="data source or catalog not found") from exc
    except HTTPException:
        raise
    except Exception as exc:
        _audit_source_action(request, "schema_catalog_enrich", source_id=source_id,
                             status="failed", detail=f"enrichment failed ({type(exc).__name__})")
        raise HTTPException(status_code=502, detail=f"语义描述生成失败（{type(exc).__name__}）") from exc
    _audit_source_action(request, "schema_catalog_enrich", source_id=source_id,
                         source_name=source["name"])
    return {"source_id": source_id, "table_count": len(catalog["tables"]),
            "generation_source": catalog["generation_source"],
            "generated_at": catalog["generated_at"]}


@app.patch("/api/data-sources/{source_id}/schema-catalog/descriptions")
def patch_schema_catalog_descriptions(
    source_id: str, payload: CatalogDescriptionPatch, request: Request
):
    require_permission(request, "data_sources.update")
    try:
        source = app.state.identity.get_source(
            request.state.user["id"], source_id, required_access="manage"
        )
        catalog = app.state.schema_catalogs.apply_descriptions(
            source_id, payload.tables, source="manual"
        )
        _invalidate_source_runtime(source_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="data source or catalog not found") from exc
    _audit_source_action(request, "schema_catalog_edit", source_id=source_id,
                         source_name=source["name"])
    return {"source_id": source_id, "generation_source": catalog["generation_source"],
            "generated_at": catalog["generated_at"]}


@app.patch("/api/data-sources/{source_id}/enabled")
def set_data_source_enabled(source_id: str, payload: DataSourceEnabledRequest, request: Request):
    require_permission(request, "data_sources.update")
    try:
        result = app.state.identity.set_source_enabled(request.state.user["id"], source_id, payload.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="data source not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    _invalidate_source_runtime(source_id)
    _audit_source_action(
        request, "enable" if payload.enabled else "disable",
        source_id=source_id, source_name=result["name"],
    )
    return result


@app.delete("/api/data-sources/{source_id}")
def delete_data_source(source_id: str, request: Request):
    require_permission(request, "data_sources.delete")
    key = (request.state.user["id"], source_id)
    try:
        source = app.state.identity.get_source(*key, include_secret=True, required_access="manage")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="data source not found") from exc
    _invalidate_source_runtime(source_id)
    if not app.state.identity.delete_source(*key):
        raise HTTPException(status_code=404, detail="data source not found")
    app.state.schema_catalogs.delete(source_id)
    config_data = source.get("config", {})
    if source["kind"] == "sqlite" and config_data.get("managed_upload"):
        upload_root = Path(app.state.settings.sqlite_upload_dir)
        if not upload_root.is_absolute():
            upload_root = ROOT / upload_root
        managed_path = Path(config_data["path"]).resolve()
        safe_root = upload_root.resolve()
        if managed_path.is_file() and managed_path.is_relative_to(safe_root):
            managed_path.unlink()
    _audit_source_action(request, "delete", source_id=source_id, source_name=source["name"])
    return {"status": "deleted", "id": source_id}


@app.get("/api/data-sources/{source_id}/grants")
def list_data_source_grants(source_id: str, request: Request):
    require_permission(request, "data_sources.share")
    try:
        return {"items": app.state.identity.list_source_grants(request.state.user["id"], source_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="data source not found") from exc


@app.put("/api/data-sources/{source_id}/grants")
def grant_data_source(source_id: str, payload: SourceGrantRequest, request: Request):
    require_permission(request, "data_sources.share")
    try:
        result = app.state.identity.grant_source(
            request.state.user["id"], source_id, payload.user_id, payload.access_level
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="data source or user not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_source_action(request, "grant", source_id=source_id, detail=f"access={payload.access_level}")
    _invalidate_source_runtime(source_id)
    return result


@app.delete("/api/data-sources/{source_id}/grants/{user_id}")
def revoke_data_source_grant(source_id: str, user_id: str, request: Request):
    require_permission(request, "data_sources.share")
    try:
        deleted = app.state.identity.revoke_source_grant(request.state.user["id"], source_id, user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="data source not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="grant not found")
    _audit_source_action(request, "revoke", source_id=source_id)
    _invalidate_source_runtime(source_id)
    return {"status": "revoked", "source_id": source_id, "user_id": user_id}


def get_source_runtime(user_id: str, source_id: str | None):
    sources = app.state.identity.list_sources(user_id)
    if not sources:
        raise HTTPException(status_code=400, detail="请先添加数据源")
    selected_id = source_id or sources[0]["id"]
    selected = next((source for source in sources if source["id"] == selected_id), None)
    if selected is None:
        raise HTTPException(status_code=404, detail="data source not found")
    if not selected["enabled"]:
        raise HTTPException(status_code=409, detail="数据源已停用")
    if selected.get("access_level") not in {"query", "manage"}:
        raise HTTPException(status_code=403, detail="data source query permission required")
    key = (user_id, selected_id)
    if key in app.state.source_runtimes:
        return app.state.source_runtimes[key]
    try:
        source = app.state.identity.get_source(user_id, selected_id, include_secret=True, required_access="query")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="data source not found") from exc
    database = build_source_database(source, app.state.settings)
    try:
        database.open()
        database.get_schema()
    except Exception as exc:
        database.close()
        raise HTTPException(status_code=400, detail=f"数据源不可用：{exc}") from exc
    retriever = None
    use_rag = source["kind"] == "sqlite" and app.state.effective_schema_context_mode == "rag"
    if use_rag or (source["kind"] == "sqlite" and app.state.settings.enable_etc):
        seed_metadata = {}
        seed_path = ROOT / "schema_metadata.json"
        if seed_path.exists():
            try:
                seed_metadata = json.loads(seed_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                seed_metadata = {}
        catalog = app.state.schema_catalogs.sync_sqlite(
            source["id"], source["config"]["path"],
            source_description=source.get("description", ""), seed_metadata=seed_metadata,
        )
        retriever = SQLiteSchemaRetriever(
            semantic_catalog=catalog,
            embedder=app.state.schema_embedder,
            embedding_weight=app.state.settings.schema_embedding_weight,
            top_tables=app.state.settings.schema_rag_top_tables,
            top_columns=app.state.settings.schema_rag_top_columns,
            max_value_samples=app.state.settings.schema_rag_value_samples,
        )
    llm = build_llm(app.state.settings)
    setattr(llm, "dialect", database.dialect)
    runtime = {
        "source": source,
        "database": database,
        "graph": build_graph(
            llm_client=llm,
            max_retries=app.state.settings.max_retries,
            execute_timeout_sec=app.state.settings.sql_timeout_sec,
            schema_retriever=retriever,
            database=database,
            max_result_rows=app.state.settings.db_max_rows,
            initial_schema_rag=use_rag,
            cost_enforce=app.state.settings.sql_cost_enforce,
            cost_max_estimated_cost=app.state.settings.sql_cost_max_estimated_cost,
            cost_max_estimated_rows=app.state.settings.sql_cost_max_estimated_rows,
            cost_max_full_scans=app.state.settings.sql_cost_max_full_scans,
            checkpointer=app.state.checkpointer,
        ),
    }
    app.state.source_runtimes[key] = runtime
    return runtime


def prepare_query(payload: QueryRequest, request: Request) -> dict[str, Any]:
    require_permission(request, "query.execute")
    request_id = str(uuid4())
    thread_id = payload.thread_id or str(uuid4())
    started_at = time.perf_counter()
    username = getattr(request.state, "authenticated_user", "anonymous")
    user = request.state.user
    runtime = get_source_runtime(user["id"], payload.data_source_id)
    source = runtime["source"]
    checkpoint_thread_id = f"{username}:{source['id']}:{thread_id}"
    graph_config = {"configurable": {"thread_id": checkpoint_thread_id}}
    settings = app.state.settings
    app.state.cancellations.create(request_id, user["id"])
    try:
        app.state.audit.begin_query(
            request_id=request_id,
            username=username,
            question=payload.question.strip(),
            provider=settings.provider,
            model=settings.model,
            database_backend=source["kind"],
            schema_context_mode=app.state.effective_schema_context_mode,
            thread_id=thread_id,
        )
    except Exception:
        logger.exception("Failed to start audit record %s", request_id)
    conversation_history = []
    checkpoint_hit = False
    if app.state.checkpointer is not None:
        snapshot = runtime["graph"].get_state(graph_config)
        checkpoint_hit = bool(snapshot.values)
        conversation_history = list(snapshot.values.get("conversation_history", []))
        conversation_history = conversation_history[-settings.session_history_turns :]
    initial_state = {
        "question": payload.question.strip(),
        "db_path": source["config"].get("path", source["kind"]),
        "retry_count": 0,
        "max_retries": app.state.settings.max_retries,
        "error_history": [],
        "retrieval_events": [],
        "conversation_history": conversation_history,
        "schema_context": "",
        "retrieval_context": "",
        "current_sql": "",
        "generation_trace": {},
        "execution_rows": [],
        "execution_columns": [],
        "execution_error": "",
        "execution_error_type": "",
        "execution_truncated": False,
        "cost_check": {},
        "cancelled": False,
        "request_id": request_id,
    }
    return {
        "request_id": request_id,
        "thread_id": thread_id,
        "started_at": started_at,
        "graph_config": graph_config,
        "initial_state": initial_state,
        "username": username,
        "user_id": user["id"],
        "question": payload.question.strip(),
        "checkpoint_hit": checkpoint_hit,
        "data_source_id": source["id"],
        "database_backend": source["kind"],
        "graph": runtime["graph"],
    }


def set_query_span(span, run: dict[str, Any], state: dict[str, Any] | None = None) -> None:
    attributes = {
        "verisql.request_id": run["request_id"],
        "langfuse.session.id": run["thread_id"],
        "user.id": run["username"],
        "verisql.database_backend": run["database_backend"],
        "verisql.schema_context_mode": app.state.effective_schema_context_mode,
        "gen_ai.request.model": app.state.settings.model,
    }
    if state is not None:
        attributes.update(
            {
                "verisql.status": state.get("status", "unknown"),
                "verisql.retry_count": state.get("retry_count", 0),
                "verisql.result_row_count": len(state.get("execution_rows", [])),
                "verisql.result_truncated": bool(state.get("execution_truncated", False)),
            }
        )
    set_span_attributes(span, attributes)


def record_query_failure(run: dict[str, Any], exc: Exception) -> None:
    try:
        app.state.audit.finish_query(
            request_id=run["request_id"],
            status="provider_error",
            error_types=[type(exc).__name__],
            latency_ms=int((time.perf_counter() - run["started_at"]) * 1000),
        )
    except Exception:
        logger.exception("Failed to finish failed audit record %s", run["request_id"])
    try:
        app.state.workbench.save_query(
            request_id=run["request_id"], user_id=run["user_id"],
            thread_id=run["thread_id"], data_source_id=run["data_source_id"],
            question=run["question"], answer="", sql="", status="provider_error",
            columns=[], rows=[], truncated=False,
            latency_ms=int((time.perf_counter() - run["started_at"]) * 1000),
        )
    except Exception:
        logger.exception("Failed to save failed workbench query %s", run["request_id"])
    app.state.cancellations.remove(run["request_id"])


def complete_query(
    run: dict[str, Any],
    state: dict[str, Any],
    timeline: list[dict[str, Any]] | None = None,
) -> QueryResponse:
    retrieval_events = list(state.get("retrieval_events", []))
    retrieved_tables = list(
        dict.fromkeys(
            table
            for event in retrieval_events
            for table in event.get("tables", [])
        )
    )
    errors = list(state.get("error_history", []))
    latency_ms = int((time.perf_counter() - run["started_at"]) * 1000)
    span_context = trace.get_current_span().get_span_context()
    trace_id = f"{span_context.trace_id:032x}" if span_context.is_valid else ""
    confidence = assess_confidence(state, app.state.settings.confidence_warning_threshold)
    cost_check = dict(state.get("cost_check", {}) or {})
    generation_trace = dict(state.get("generation_trace", {}) or {})
    usage = dict(generation_trace.get("token_usage", {}) or {})
    try:
        app.state.audit.finish_query(
            request_id=run["request_id"],
            status=state.get("status", "unknown"),
            sql=state.get("current_sql", ""),
            retry_count=state.get("retry_count", 0),
            error_types=[item.get("error_type", "unknown") for item in errors],
            retrieved_tables=retrieved_tables,
            result_row_count=len(state.get("execution_rows", [])),
            result_truncated=bool(state.get("execution_truncated", False)),
            latency_ms=latency_ms,
            trace_id=trace_id,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            total_tokens=int(usage.get("total_tokens", 0) or 0),
            model_calls=int(generation_trace.get("model_calls", 0) or 0),
            confidence_score=confidence["score"],
            confidence_level=confidence["level"],
            cost_level=str(cost_check.get("level", "")),
            estimated_cost=cost_check.get("estimated_cost"),
            cancelled=state.get("status") == "cancelled",
            timeline=list(timeline or []),
        )
    except Exception:
        logger.exception("Failed to finish audit record %s", run["request_id"])

    try:
        app.state.workbench.save_query(
            request_id=run["request_id"], user_id=run["user_id"],
            thread_id=run["thread_id"], data_source_id=run["data_source_id"],
            question=run["question"], answer=state.get("final_answer", ""),
            sql=state.get("current_sql", ""), status=state.get("status", "unknown"),
            columns=list(state.get("execution_columns", [])),
            rows=[list(row) for row in state.get("execution_rows", [])],
            truncated=bool(state.get("execution_truncated", False)), latency_ms=latency_ms,
        )
    except Exception:
        logger.exception("Failed to save workbench query %s", run["request_id"])

    response = QueryResponse(
        request_id=run["request_id"],
        thread_id=run["thread_id"],
        status=state.get("status", "unknown"),
        answer=state.get("final_answer", ""),
        sql=state.get("current_sql", ""),
        columns=list(state.get("execution_columns", [])),
        rows=[list(row) for row in state.get("execution_rows", [])],
        retry_count=state.get("retry_count", 0),
        errors=errors,
        retrieval_events=retrieval_events,
        generation_trace=generation_trace,
        ast_validation=dict(state.get("ast_validation", {})),
        truncated=bool(state.get("execution_truncated", False)),
        trace_id=trace_id,
        latency_ms=latency_ms,
        timeline=list(timeline or []),
        checkpoint_hit=bool(run.get("checkpoint_hit", False)),
        data_source_id=run["data_source_id"],
        confidence=confidence,
        cost_check=cost_check,
    )
    app.state.cancellations.remove(run["request_id"])
    return response


@app.post("/api/query", response_model=QueryResponse)
def query(payload: QueryRequest, request: Request):
    with tracer.start_as_current_span("verisql.query") as span:
        run = prepare_query(payload, request)
        set_query_span(span, run)
        try:
            state = run["graph"].invoke(
                run["initial_state"], config=run["graph_config"]
            )
        except Exception as exc:  # keep provider details server-side in real deployments
            record_query_failure(run, exc)
            raise HTTPException(
                status_code=502,
                detail=f"Agent execution failed: {exc}; request_id={run['request_id']}",
            ) from exc
        set_query_span(span, run, state)
        return complete_query(run, state)


@app.post("/api/query/stream")
async def query_stream(payload: QueryRequest, request: Request):
    run = prepare_query(payload, request)

    async def stream_events():
        yield encode_sse(
            "start",
            {
                "request_id": run["request_id"],
                "thread_id": run["thread_id"],
                "message": "Agent 已开始运行",
            },
        )
        queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def publish(event: str, data: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, (event, data))

        def worker() -> None:
            state = dict(run["initial_state"])
            sequence = 0
            timeline: list[dict[str, Any]] = []
            last_step_at = time.perf_counter()
            with tracer.start_as_current_span("verisql.query.stream") as span:
                set_query_span(span, run)
                span.set_attribute("verisql.transport", "sse")
                try:
                    for update in run["graph"].stream(
                        run["initial_state"],
                        config=run["graph_config"],
                        stream_mode="updates",
                    ):
                        for node, values in update.items():
                            if isinstance(values, dict):
                                state.update(values)
                            sequence += 1
                            now = time.perf_counter()
                            duration_ms = int((now - last_step_at) * 1000)
                            last_step_at = now
                            step = {
                                "sequence": sequence,
                                "node": node,
                                "message": NODE_LABELS.get(node, "Agent 正在处理"),
                                "duration_ms": duration_ms,
                                "status": "completed",
                            }
                            timeline.append(step)
                            publish(
                                "progress",
                                step,
                            )
                    set_query_span(span, run, state)
                    response = complete_query(run, state, timeline)
                    publish("final", response.model_dump(mode="json"))
                except Exception as exc:
                    logger.exception("Streaming query failed: %s", run["request_id"])
                    record_query_failure(run, exc)
                    span.set_attribute("verisql.status", "provider_error")
                    publish(
                        "error",
                        {
                            "request_id": run["request_id"],
                            "thread_id": run["thread_id"],
                            "detail": "Agent execution failed",
                        },
                    )
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, None)

        worker_task = asyncio.create_task(asyncio.to_thread(worker))
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if item is None:
                    break
                event, data = item
                yield encode_sse(event, data)
        finally:
            if not worker_task.done():
                try:
                    app.state.cancellations.cancel(run["request_id"], run["user_id"])
                except (PermissionError, KeyError):
                    pass
                worker_task.add_done_callback(lambda task: task.exception())
            else:
                worker_task.result()

    return StreamingResponse(
        stream_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/requests/{request_id}/cancel")
def cancel_request(request_id: str, request: Request):
    require_permission(request, "query.execute")
    try:
        cancelled = app.state.cancellations.cancel(
            request_id, request.state.user["id"],
            is_admin=app.state.identity.has_permission(request.state.user["id"], "audit.read"),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not cancelled:
        raise HTTPException(status_code=404, detail="request is not running")
    return {"status": "cancellation_requested", "request_id": request_id}


@app.post("/api/feedback")
def feedback(payload: FeedbackRequest, request: Request):
    require_permission(request, "feedback.create")
    username = getattr(request.state, "authenticated_user", "anonymous")
    try:
        app.state.audit.add_feedback(
            payload.request_id,
            username,
            payload.verdict,
            payload.comment.strip(),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="request_id not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "saved", "request_id": payload.request_id}


@app.get("/api/audit/queries")
def audit_queries(request: Request, limit: int = 50):
    require_permission(request, "audit.read")
    if not app.state.settings.audit_enabled:
        raise HTTPException(status_code=503, detail="audit logging is disabled")
    return {"items": app.state.audit.list_queries(limit=limit)}


@app.get("/api/audit/resources")
def audit_resources(request: Request, limit: int = 100):
    require_permission(request, "audit.read")
    if not app.state.settings.audit_enabled:
        raise HTTPException(status_code=503, detail="audit logging is disabled")
    return {"items": app.state.audit.list_resource_actions(limit=limit)}


@app.get("/api/admin/overview")
def admin_overview(request: Request):
    require_permission(request, "audit.read")
    return app.state.audit.overview()


@app.get("/api/admin/traces")
def admin_traces(request: Request, limit: int = 100):
    require_permission(request, "audit.read")
    return {"items": app.state.audit.list_queries(limit=limit)}


@app.get("/api/admin/feedback")
def admin_feedback(request: Request, limit: int = 100):
    require_permission(request, "audit.read")
    return {"items": app.state.audit.list_feedback(limit=limit)}


@app.get("/api/admin/evaluation")
def admin_evaluation(request: Request):
    require_permission(request, "audit.read")
    overview = app.state.audit.overview()
    return {
        "mode": "feedback_verified_online_evaluation",
        "rated_queries": overview.get("feedback_total", 0),
        "correct": overview.get("feedback_correct", 0),
        "incorrect": overview.get("feedback_incorrect", 0),
        "verified_accuracy": overview.get("verified_accuracy"),
        "note": "准确率仅以用户已反馈样本为分母，不代表 BIRD 离线 Execution Accuracy。",
    }


@app.get("/api/sessions/{thread_id}")
def get_session(thread_id: str, request: Request, data_source_id: str | None = None):
    require_permission(request, "sessions.manage")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
        raise HTTPException(status_code=422, detail="invalid thread_id")
    if app.state.checkpointer is None:
        raise HTTPException(status_code=503, detail="checkpoint persistence is disabled")
    username = getattr(request.state, "authenticated_user", "anonymous")
    runtime = get_source_runtime(request.state.user["id"], data_source_id)
    snapshot = runtime["graph"].get_state(
        {"configurable": {"thread_id": f"{username}:{runtime['source']['id']}:{thread_id}"}}
    )
    history = list(snapshot.values.get("conversation_history", []))
    try:
        queries = app.state.workbench.list_queries(
            request.state.user["id"], thread_id, runtime["source"]["id"]
        )
    except KeyError:
        queries = []
    return {"thread_id": thread_id, "history": history, "queries": queries}


@app.delete("/api/sessions/{thread_id}")
def delete_session(thread_id: str, request: Request, data_source_id: str | None = None):
    require_permission(request, "sessions.manage")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
        raise HTTPException(status_code=422, detail="invalid thread_id")
    if app.state.checkpointer is None:
        raise HTTPException(status_code=503, detail="checkpoint persistence is disabled")
    username = getattr(request.state, "authenticated_user", "anonymous")
    sources = app.state.identity.list_sources(request.state.user["id"])
    selected_id = data_source_id or (sources[0]["id"] if sources else None)
    if not selected_id or not any(source["id"] == selected_id for source in sources):
        raise HTTPException(status_code=404, detail="data source not found")
    app.state.checkpointer.delete_thread(f"{username}:{selected_id}:{thread_id}")
    app.state.workbench.delete_session(
        request.state.user["id"], thread_id, selected_id
    )
    return {"status": "deleted", "thread_id": thread_id}


@app.get("/api/workbench/sessions")
def list_workbench_sessions(request: Request, data_source_id: str | None = None, limit: int = 50):
    require_permission(request, "sessions.manage")
    return {"items": app.state.workbench.list_sessions(request.state.user["id"], data_source_id, limit)}


@app.get("/api/workbench/sessions/{thread_id}/queries")
def list_workbench_queries(thread_id: str, request: Request, data_source_id: str):
    require_permission(request, "sessions.manage")
    try:
        items = app.state.workbench.list_queries(request.state.user["id"], thread_id, data_source_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    return {"items": items}


@app.patch("/api/workbench/sessions/{thread_id}")
def rename_workbench_session(
    thread_id: str, payload: SessionRenameRequest, request: Request, data_source_id: str,
):
    require_permission(request, "sessions.manage")
    try:
        return app.state.workbench.rename_session(
            request.state.user["id"], thread_id, data_source_id, payload.title.strip()
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc


@app.get("/api/query-results/{request_id}")
def paginated_query_result(
    request_id: str, request: Request, page: int = 1, page_size: int = 50,
):
    require_permission(request, "query.execute")
    safe_page = max(1, page)
    safe_page_size = min(max(1, page_size), 200)
    try:
        result = app.state.workbench.get_result(request.state.user["id"], request_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="result expired or not found") from exc
    total = len(result["rows"])
    start = (safe_page - 1) * safe_page_size
    return {
        "request_id": request_id, "thread_id": result["thread_id"],
        "data_source_id": result["data_source_id"], "question": result["question"],
        "answer": result["answer"], "sql": result["sql"], "status": result["status"],
        "columns": result["columns"], "rows": result["rows"][start:start + safe_page_size],
        "page": safe_page, "page_size": safe_page_size, "total_rows": total,
        "total_pages": max(1, (total + safe_page_size - 1) // safe_page_size),
        "truncated": result["truncated"], "latency_ms": result["latency_ms"],
    }


def _csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


@app.get("/api/query-results/{request_id}/export.csv")
def export_query_result_csv(request_id: str, request: Request):
    require_permission(request, "query.execute")
    try:
        result = app.state.workbench.get_result(request.state.user["id"], request_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="result expired or not found") from exc
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(result["columns"])
    writer.writerows([_csv_safe(cell) for cell in row] for row in result["rows"])
    return Response(
        content="\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="verisql-{request_id}.csv"'},
    )
