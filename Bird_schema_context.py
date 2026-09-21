"""
Shared BIRD schema context builder.

The basic mode matches the original schema-only prompt. The enhanced mode adds
concise metadata from BIRD database_description CSV files, especially
value_description, to reduce schema/value grounding errors without retraining.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any


SchemaMode = str


def build_schema_context(
    table_info: dict[str, Any],
    db_root: Path | None = None,
    db_id: str | None = None,
    mode: SchemaMode = "basic",
    max_extra_chars_per_column: int = 220,
    max_total_extra_chars: int = 2500,
) -> str:
    table_names = table_info["table_names_original"]
    column_names_original = table_info["column_names_original"]
    column_names = table_info.get("column_names", column_names_original)
    column_types = table_info["column_types"]

    descriptions = {}
    if mode == "enhanced" and db_root is not None and db_id is not None:
        descriptions = load_database_descriptions(str(db_root), db_id)
        value_samples = load_value_samples(str(db_root), db_id)
        for table_name, column_map in value_samples.items():
            descriptions.setdefault(table_name, {})
            for column_name, samples in column_map.items():
                descriptions[table_name].setdefault(column_name, {})
                descriptions[table_name][column_name]["sample_values"] = samples

    table_columns: dict[int, list[str]] = {idx: [] for idx in range(len(table_names))}
    for col_idx, (table_idx, col_name) in enumerate(column_names_original):
        if table_idx < 0:
            continue

        table_name = table_names[table_idx]
        natural_name = column_names[col_idx][1] if col_idx < len(column_names) else col_name
        col_type = column_types[col_idx] if col_idx < len(column_types) else "unknown"

        line = f"`{col_name}` {col_type}"
        if natural_name != col_name:
            line += f" -- {format_column_hint(natural_name)}"

        if mode == "enhanced":
            extra = build_column_extra(
                descriptions.get(table_name.lower(), {}).get(col_name.lower(), {}),
                natural_name=natural_name,
                col_name=col_name,
                max_chars=max_extra_chars_per_column,
            )
            if extra:
                line += f" | {extra}"

        table_columns[table_idx].append(line)

    if mode == "enhanced" and max_total_extra_chars > 0:
        table_columns = fit_extra_budget(table_columns, max_total_extra_chars)

    lines = []
    for table_idx, table_name in enumerate(table_names):
        lines.append(f"表 `{table_name}`:")
        for col in table_columns.get(table_idx, []):
            lines.append(f"  - {col}")

    foreign_keys = table_info.get("foreign_keys", [])
    if foreign_keys:
        lines.append("外键关系:")
        for src_idx, dst_idx in foreign_keys:
            src_table_idx, src_col = column_names_original[src_idx]
            dst_table_idx, dst_col = column_names_original[dst_idx]
            lines.append(
                f"  - `{table_names[src_table_idx]}`.`{src_col}` = "
                f"`{table_names[dst_table_idx]}`.`{dst_col}`"
            )

    return "\n".join(lines)


def fit_extra_budget(
    table_columns: dict[int, list[str]],
    max_total_extra_chars: int,
) -> dict[int, list[str]]:
    extra_total = 0
    for cols in table_columns.values():
        for line in cols:
            if " | " in line:
                extra_total += len(line.split(" | ", 1)[1])
    if extra_total <= max_total_extra_chars:
        return table_columns

    fitted: dict[int, list[str]] = {}
    used = 0
    for table_idx, cols in table_columns.items():
        fitted_cols = []
        for line in cols:
            if " | " not in line:
                fitted_cols.append(line)
                continue
            base, extra = line.split(" | ", 1)
            remaining = max_total_extra_chars - used
            if remaining <= 0:
                fitted_cols.append(base)
                continue
            if len(extra) <= remaining:
                fitted_cols.append(line)
                used += len(extra)
            elif remaining >= 60:
                shortened = truncate_text(extra, remaining)
                fitted_cols.append(f"{base} | {shortened}")
                used += len(shortened)
            else:
                fitted_cols.append(base)
        fitted[table_idx] = fitted_cols
    return fitted


@lru_cache(maxsize=256)
def load_database_descriptions(db_root: str, db_id: str) -> dict[str, dict[str, dict[str, str]]]:
    description_dir = Path(db_root) / db_id / "database_description"
    if not description_dir.exists():
        return {}

    table_map: dict[str, dict[str, dict[str, str]]] = {}
    for csv_path in description_dir.glob("*.csv"):
        table_name = csv_path.stem.lower()
        column_map: dict[str, dict[str, str]] = {}
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    original = clean_text(row.get("original_column_name", ""))
                    if not original:
                        continue
                    column_map[original.lower()] = {
                        "column_name": clean_text(row.get("column_name", "")),
                        "column_description": clean_text(row.get("column_description", "")),
                        "value_description": clean_text(
                            row.get("value_description", "") or row.get("", "") or row.get(None, "")
                        ),
                    }
        except UnicodeDecodeError:
            with csv_path.open("r", encoding="latin-1", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    original = clean_text(row.get("original_column_name", ""))
                    if not original:
                        continue
                    column_map[original.lower()] = {
                        "column_name": clean_text(row.get("column_name", "")),
                        "column_description": clean_text(row.get("column_description", "")),
                        "value_description": clean_text(
                            row.get("value_description", "") or row.get("", "") or row.get(None, "")
                        ),
                    }
        if column_map:
            table_map[table_name] = column_map
    return table_map


@lru_cache(maxsize=256)
def load_value_samples(
    db_root: str,
    db_id: str,
    max_samples_per_column: int = 8,
    max_sample_chars: int = 40,
) -> dict[str, dict[str, str]]:
    db_path = Path(db_root) / db_id / f"{db_id}.sqlite"
    if not db_path.exists():
        return {}

    result: dict[str, dict[str, str]] = {}
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return {}

    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table_name in tables:
            table_result = {}
            columns = conn.execute(f"PRAGMA table_info({quote_identifier(table_name)})").fetchall()
            for column in columns:
                column_name = column[1]
                column_type = str(column[2] or "").lower()
                if not should_sample_values(column_type, column_name):
                    continue
                samples = fetch_distinct_samples(
                    conn,
                    table_name,
                    column_name,
                    max_samples=max_samples_per_column,
                    max_sample_chars=max_sample_chars,
                )
                if samples:
                    table_result[column_name.lower()] = ", ".join(samples)
            if table_result:
                result[table_name.lower()] = table_result
    except sqlite3.Error:
        return result
    finally:
        conn.close()

    return result


def build_column_extra(
    description: dict[str, str],
    natural_name: str,
    col_name: str,
    max_chars: int,
) -> str:
    value_desc = description.get("value_description", "")
    column_desc = description.get("column_description", "")
    described_name = description.get("column_name", "")
    sample_values = description.get("sample_values", "")

    pieces = []
    if described_name and described_name not in {natural_name, col_name}:
        pieces.append(f"name: {described_name}")
    if column_desc and is_informative_description(column_desc, natural_name, col_name):
        pieces.append(f"desc: {column_desc}")
    if value_desc and value_desc.lower() not in {"not useful", "not quite useful"}:
        pieces.append(f"values: {value_desc}")
    if sample_values:
        pieces.append(f"samples: {sample_values}")

    if not pieces:
        return ""
    return truncate_text("; ".join(pieces), max_chars)


def is_informative_description(text: str, natural_name: str, col_name: str) -> bool:
    lowered = text.lower().strip(" .,")
    if not lowered:
        return False
    trivial = {
        natural_name.lower().strip(" .,"),
        col_name.lower().strip(" .,"),
        f"the {natural_name}".lower().strip(" .,"),
        f"the {col_name}".lower().strip(" .,"),
    }
    return lowered not in trivial


def clean_text(text: Any) -> str:
    if text is None:
        return ""
    if isinstance(text, list):
        text = " ".join(str(item) for item in text if item is not None)
    cleaned = str(text).replace("\ufeff", "").replace("\x95", "-")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


def should_sample_values(column_type: str, column_name: str) -> bool:
    lowered_name = column_name.lower()
    if "id" in lowered_name and lowered_name not in {"grid"}:
        return False
    return any(token in column_type for token in ["char", "text", "date", "time", "varchar"])


def fetch_distinct_samples(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    max_samples: int,
    max_sample_chars: int,
) -> list[str]:
    table_sql = quote_identifier(table_name)
    column_sql = quote_identifier(column_name)
    query = (
        f"SELECT DISTINCT {column_sql} FROM {table_sql} "
        f"WHERE {column_sql} IS NOT NULL AND CAST({column_sql} AS TEXT) != '' "
        f"LIMIT {max_samples}"
    )
    samples = []
    try:
        for (value,) in conn.execute(query):
            text = clean_text(value)
            if not text:
                continue
            samples.append(repr_sql_value(truncate_text(text, max_sample_chars)))
    except sqlite3.Error:
        return []
    return samples


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def repr_sql_value(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def format_column_hint(name: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return name
    return f"`{name.replace('`', '``')}`"
