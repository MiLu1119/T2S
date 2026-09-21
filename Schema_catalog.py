"""Persistent, per-data-source semantic catalog used by Schema retrieval."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any


_BUSINESS_ALIASES = {
    "config": ["配置", "参数", "设置"], "setting": ["配置", "设置"],
    "system": ["系统"], "sys": ["系统"], "user": ["用户"],
    "employee": ["员工", "人员"], "department": ["部门", "组织"],
    "dept": ["部门"], "order": ["订单"], "sale": ["销售"],
    "sales": ["销售"], "amount": ["金额", "销售额"], "price": ["价格"],
    "status": ["状态"], "state": ["状态"], "enabled": ["启用", "有效"],
    "name": ["名称", "姓名"], "count": ["数量", "个数"],
    "created": ["创建"], "updated": ["更新"], "date": ["日期"],
    "time": ["时间"], "type": ["类型"], "category": ["分类"],
    "product": ["产品", "商品"], "customer": ["客户"], "region": ["地区", "区域"],
}


def _words(identifier: str) -> list[str]:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", identifier).lower()
    return [part for part in re.split(r"[^a-z0-9\u4e00-\u9fff]+", value) if part]


def identifier_description(identifier: str) -> str:
    words = _words(identifier)
    aliases = []
    for word in words:
        aliases.extend(_BUSINESS_ALIASES.get(word, []))
        if word.endswith("s"):
            aliases.extend(_BUSINESS_ALIASES.get(word[:-1], []))
    readable = " ".join(words)
    return "、".join(dict.fromkeys([readable, *aliases]))


class SchemaCatalogStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_catalogs (
                    source_id TEXT PRIMARY KEY,
                    schema_hash TEXT NOT NULL,
                    catalog_json TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    generation_source TEXT NOT NULL
                )"""
            )

    def get(self, source_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT catalog_json FROM schema_catalogs WHERE source_id = ?", (source_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def delete(self, source_id: str) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM schema_catalogs WHERE source_id = ?", (source_id,))

    def apply_descriptions(
        self, source_id: str, updates: dict[str, Any], *, source: str = "manual"
    ) -> dict[str, Any]:
        catalog = self.get(source_id)
        if catalog is None:
            raise KeyError(source_id)
        for table_name, table_update in updates.items():
            table = catalog["tables"].get(table_name)
            if table is None or not isinstance(table_update, dict):
                continue
            description = str(table_update.get("description", "")).strip()[:500]
            if description:
                table["description"] = description
                table["description_source"] = source
            for column_name, raw_description in table_update.get("columns", {}).items():
                column = table["columns"].get(column_name)
                if column is None:
                    continue
                description = str(raw_description).strip()[:500]
                if description:
                    column["description"] = description
                    column["description_source"] = source
        catalog["generated_at"] = datetime.now(timezone.utc).isoformat()
        catalog["generation_source"] = source
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """UPDATE schema_catalogs SET catalog_json = ?, generated_at = ?,
                   generation_source = ? WHERE source_id = ?""",
                (json.dumps(catalog, ensure_ascii=False), catalog["generated_at"], source, source_id),
            )
        return catalog

    @staticmethod
    def model_payload(catalog: dict[str, Any], table_names: list[str] | None = None) -> list[dict]:
        selected = set(table_names or catalog["tables"].keys())
        relations = catalog.get("foreign_keys", []) + catalog.get("inferred_relations", [])
        payload = []
        for name, table in catalog["tables"].items():
            if name not in selected:
                continue
            payload.append({
                "name": name,
                "columns": [
                    {"name": column_name, "type": column["data_type"],
                     "primary_key": column.get("primary_key", False)}
                    for column_name, column in table["columns"].items()
                ],
                "relations": [item for item in relations
                              if item.get("source_table") == name or item.get("target_table") == name],
            })
        return payload

    def sync_sqlite(
        self, source_id: str, db_path: str | Path, *, source_description: str = "",
        seed_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        physical = self._inspect_sqlite(db_path)
        schema_hash = hashlib.sha256(
            json.dumps(physical, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        existing = self.get(source_id)
        if existing and existing.get("schema_hash") == schema_hash:
            return existing
        seeds = (seed_metadata or {}).get("tables", {})
        tables: dict[str, Any] = {}
        for table_name, table in physical["tables"].items():
            seed = seeds.get(table_name, {})
            columns = {}
            for column in table["columns"]:
                seed_description = seed.get("columns", {}).get(column["name"], "")
                columns[column["name"]] = {
                    **column,
                    "description": seed_description or identifier_description(column["name"]),
                    "description_source": "seed" if seed_description else "inferred",
                }
            tables[table_name] = {
                "description": seed.get("description") or identifier_description(table_name),
                "description_source": "seed" if seed.get("description") else "inferred",
                "columns": columns,
                "value_grounding_columns": list(seed.get("value_grounding_columns", [])),
                "value_descriptions": dict(seed.get("value_descriptions", {})),
            }
        inferred = self._infer_relations(physical)
        catalog = {
            "source_id": source_id, "schema_hash": schema_hash,
            "source_description": source_description,
            "tables": tables, "foreign_keys": physical["foreign_keys"],
            "inferred_relations": inferred,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generation_source": "database+seed+identifier_inference",
        }
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO schema_catalogs
                   (source_id, schema_hash, catalog_json, generated_at, generation_source)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(source_id) DO UPDATE SET schema_hash=excluded.schema_hash,
                     catalog_json=excluded.catalog_json, generated_at=excluded.generated_at,
                     generation_source=excluded.generation_source""",
                (source_id, schema_hash, json.dumps(catalog, ensure_ascii=False),
                 catalog["generated_at"], catalog["generation_source"]),
            )
        return catalog

    @staticmethod
    def _inspect_sqlite(db_path: str | Path) -> dict[str, Any]:
        conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
        try:
            names = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'"
            )]
            tables, foreign_keys = {}, []
            for name in names:
                columns = [{"name": str(row[1]), "data_type": str(row[2] or "unknown"),
                            "nullable": not bool(row[3]), "primary_key": bool(row[5])}
                           for row in conn.execute(f"PRAGMA table_info({json.dumps(name)})")]
                tables[name] = {"columns": columns}
                for row in conn.execute(f"PRAGMA foreign_key_list({json.dumps(name)})"):
                    foreign_keys.append({"source_table": name, "source_column": str(row[3]),
                                         "target_table": str(row[2]), "target_column": str(row[4]),
                                         "source": "declared", "confidence": 1.0})
            return {"tables": tables, "foreign_keys": foreign_keys}
        finally:
            conn.close()

    @staticmethod
    def _infer_relations(schema: dict[str, Any]) -> list[dict[str, Any]]:
        declared = {(fk["source_table"], fk["source_column"]) for fk in schema["foreign_keys"]}
        primary = {}
        for table, info in schema["tables"].items():
            for column in info["columns"]:
                if column["primary_key"]:
                    primary.setdefault(column["name"].lower(), []).append((table, column))
        inferred = []
        for table, info in schema["tables"].items():
            for column in info["columns"]:
                if (table, column["name"]) in declared or not column["name"].lower().endswith("_id"):
                    continue
                stem = column["name"][:-3].lower()
                candidates = []
                for target, target_info in schema["tables"].items():
                    singular = target.lower().rstrip("s")
                    for pk in target_info["columns"]:
                        if pk["primary_key"] and pk["data_type"].upper() == column["data_type"].upper():
                            if singular == stem or pk["name"].lower() == column["name"].lower():
                                candidates.append((target, pk["name"]))
                if len(candidates) == 1 and candidates[0][0] != table:
                    inferred.append({"source_table": table, "source_column": column["name"],
                                     "target_table": candidates[0][0], "target_column": candidates[0][1],
                                     "source": "naming_inference", "confidence": 0.8})
        return inferred
