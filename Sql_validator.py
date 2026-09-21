"""SQLGlot AST safety and SQLite schema validation.

This is the pre-execution gate for the online Agent. The database is still
opened read-only by Executor.py, so validation and execution form two layers.
"""

from __future__ import annotations

import difflib
import sqlite3
from dataclasses import asdict, dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


@dataclass
class SQLValidationResult:
    is_valid: bool
    error_type: str = ""
    reason: str = ""
    used_tables: list[str] = field(default_factory=list)
    used_columns: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


_DANGEROUS_NODES = tuple(
    node
    for node in (
        getattr(exp, "Insert", None),
        getattr(exp, "Update", None),
        getattr(exp, "Delete", None),
        getattr(exp, "Drop", None),
        getattr(exp, "Create", None),
        getattr(exp, "Alter", None),
        getattr(exp, "Merge", None),
        getattr(exp, "Command", None),
        getattr(exp, "Transaction", None),
    )
    if node is not None
)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def load_sqlite_schema(db_path: str) -> dict[str, set[str]]:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        schema: dict[str, set[str]] = {}
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (table_name,) in tables:
            columns = conn.execute(f"PRAGMA table_info({_quote(table_name)})").fetchall()
            schema[table_name.lower()] = {str(column[1]).lower() for column in columns}
        return schema
    finally:
        conn.close()


def validate_sql_ast(
    sql: str,
    db_path: str | None = None,
    dialect: str = "sqlite",
    schema: dict[str, set[str]] | None = None,
) -> SQLValidationResult:
    if not sql or not sql.strip():
        return SQLValidationResult(False, "empty_sql", "SQL is empty")

    try:
        statements = sqlglot.parse(sql, read=dialect)
    except ParseError as exc:
        return SQLValidationResult(False, "syntax", f"SQL parse failed: {exc}")
    except Exception as exc:  # defensive: validation must never crash the graph
        return SQLValidationResult(False, "syntax", f"SQL parse failed: {exc}")

    statements = [statement for statement in statements if statement is not None]
    if len(statements) != 1:
        return SQLValidationResult(
            False,
            "stacked_query",
            f"exactly one SQL statement is allowed; received {len(statements)}",
        )

    tree = statements[0]
    dangerous = next((node for node in tree.walk() if isinstance(node, _DANGEROUS_NODES)), None)
    if dangerous is not None:
        return SQLValidationResult(
            False,
            "unsafe_statement",
            f"disallowed AST node: {dangerous.__class__.__name__}",
        )
    if not isinstance(tree, exp.Query):
        return SQLValidationResult(
            False,
            "unsafe_statement",
            f"only read-only query statements are allowed; received {tree.__class__.__name__}",
        )

    if schema is None:
        if not db_path:
            return SQLValidationResult(False, "schema_load", "db_path or schema is required")
        try:
            schema = load_sqlite_schema(db_path)
        except sqlite3.Error as exc:
            return SQLValidationResult(False, "schema_load", f"failed to load database schema: {exc}")
    schema = {name.lower(): {column.lower() for column in columns} for name, columns in schema.items()}

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE) if cte.alias_or_name}
    physical_tables: list[exp.Table] = [
        table for table in tree.find_all(exp.Table) if table.name.lower() not in cte_names
    ]
    used_tables = sorted(
        {
            f"{table.db}.{table.name}" if table.db else table.name
            for table in physical_tables
        }
    )
    alias_to_table: dict[str, str] = {}
    for table in physical_tables:
        short_name = table.name.lower()
        requested_name = f"{table.db}.{table.name}".lower() if table.db else short_name
        table_name = requested_name
        if table_name not in schema and not table.db:
            matches = [name for name in schema if name.rsplit(".", 1)[-1] == short_name]
            if len(matches) == 1:
                table_name = matches[0]
            elif len(matches) > 1:
                return SQLValidationResult(
                    False,
                    "ambiguous_table",
                    f"table `{table.name}` exists in multiple schemas; qualify it explicitly",
                    used_tables=used_tables,
                    suggestions=sorted(matches),
                )
        if table_name not in schema:
            suggestions = difflib.get_close_matches(requested_name, schema.keys(), n=3, cutoff=0.45)
            return SQLValidationResult(
                False,
                "unknown_table",
                f"unknown table `{requested_name}`",
                used_tables=used_tables,
                suggestions=suggestions,
            )
        alias_to_table[(table.alias_or_name or table.name).lower()] = table_name
        alias_to_table.setdefault(short_name, table_name)
        alias_to_table.setdefault(requested_name, table_name)

    select_aliases = {
        alias.alias.lower()
        for alias in tree.find_all(exp.Alias)
        if alias.alias
    }
    used_columns: list[str] = []
    all_physical_columns = set().union(*(schema[name] for name in alias_to_table.values())) if alias_to_table else set()
    for column in tree.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            continue
        column_name = column.name.lower()
        qualifier = column.table.lower() if column.table else ""
        display_name = f"{column.table}.{column.name}" if column.table else column.name
        used_columns.append(display_name)

        if qualifier:
            physical_table = alias_to_table.get(qualifier)
            # A qualifier can refer to a CTE or derived table. Its output schema
            # requires scope-aware lineage analysis, so leave it to SQLite.
            if physical_table is None:
                continue
            if column_name not in schema[physical_table]:
                suggestions = difflib.get_close_matches(
                    column_name, schema[physical_table], n=3, cutoff=0.45
                )
                return SQLValidationResult(
                    False,
                    "unknown_column",
                    f"unknown column `{column.name}` on table `{physical_table}`",
                    used_tables=used_tables,
                    used_columns=sorted(set(used_columns)),
                    suggestions=suggestions,
                )
        elif (
            column_name not in all_physical_columns
            and column_name not in select_aliases
            and physical_tables
        ):
            candidates = sorted(all_physical_columns)
            suggestions = difflib.get_close_matches(column_name, candidates, n=3, cutoff=0.45)
            return SQLValidationResult(
                False,
                "unknown_column",
                f"unknown column `{column.name}` in referenced tables",
                used_tables=used_tables,
                used_columns=sorted(set(used_columns)),
                suggestions=suggestions,
            )

    return SQLValidationResult(
        True,
        used_tables=used_tables,
        used_columns=sorted(set(used_columns)),
    )
