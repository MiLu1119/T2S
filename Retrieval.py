"""Hybrid Schema RAG for SQLite Text-to-SQL.

The default retriever combines a BM25-style lexical score with character
n-gram vector similarity, reranks columns, completes missing foreign-key paths,
and samples bounded categorical values for value grounding. It intentionally
has no model/API dependency, making the retrieval baseline reproducible.
"""

from __future__ import annotations

import json
import hashlib
import math
import re
import sqlite3
import numpy as np
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _tokens(text: str) -> list[str]:
    normalized = text.lower().replace("_", " ")
    words = re.findall(r"[a-z0-9]+", normalized)
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    bigrams = ([chinese] if len(chinese) == 1 else
               [chinese[i : i + 2] for i in range(max(0, len(chinese) - 1))])
    return words + bigrams


def _char_ngrams(text: str, n: int = 3) -> Counter[str]:
    normalized = re.sub(r"\s+", " ", text.lower().replace("_", " ")).strip()
    if len(normalized) < n:
        return Counter([normalized]) if normalized else Counter()
    return Counter(normalized[i : i + n] for i in range(len(normalized) - n + 1))


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    overlap = sum(value * right.get(key, 0) for key, value in left.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return overlap / (left_norm * right_norm) if left_norm and right_norm else 0.0


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    data_type: str
    description: str = ""
    primary_key: bool = False

    def document(self, table_name: str) -> str:
        return f"{table_name} {self.name} {self.data_type} {self.description}".strip()


@dataclass(frozen=True)
class ForeignKeyInfo:
    source_table: str
    source_column: str
    target_table: str
    target_column: str


@dataclass
class TableInfo:
    name: str
    description: str
    columns: list[ColumnInfo]

    def document(self) -> str:
        column_text = " ".join(column.document(self.name) for column in self.columns)
        return f"{self.name} {self.description} {column_text}".strip()


@dataclass
class SchemaCatalog:
    tables: dict[str, TableInfo]
    foreign_keys: list[ForeignKeyInfo]


@dataclass
class RetrievalResult:
    context: str
    tables: list[str]
    reason: str
    initial_tables: list[str] = field(default_factory=list)
    bridge_tables: list[str] = field(default_factory=list)
    columns: dict[str, list[str]] = field(default_factory=dict)
    foreign_key_paths: list[list[str]] = field(default_factory=list)
    grounded_values: dict[str, list[str]] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)

    def to_event(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("context", None)
        return data


class SQLiteSchemaRetriever:
    """Dependency-free hybrid Schema RAG baseline for SQLite."""

    def __init__(
        self,
        metadata_path: str | Path | None = None,
        semantic_catalog: dict[str, Any] | None = None,
        embedder: Any | None = None,
        embedding_weight: float = 0.45,
        top_tables: int = 3,
        top_columns: int = 8,
        max_value_samples: int = 5,
        lexical_weight: float = 0.65,
        vector_weight: float = 0.35,
    ):
        self.metadata_path = Path(metadata_path) if metadata_path else Path(__file__).with_name("schema_metadata.json")
        self.semantic_catalog = semantic_catalog
        self.embedder = embedder
        self.embedding_weight = min(0.8, max(0.0, embedding_weight))
        self.top_tables = max(1, top_tables)
        self.top_columns = max(1, top_columns)
        self.max_value_samples = max(0, max_value_samples)
        self.lexical_weight = lexical_weight
        self.vector_weight = vector_weight
        self._catalog_cache: dict[str, SchemaCatalog] = {}
        self._embedding_cache: dict[str, tuple[list[str], np.ndarray]] = {}

    def retrieve(
        self,
        db_path: str,
        question: str,
        sql_prefix: str = "",
        top_k: int | None = None,
        reason: str = "initial schema retrieval",
    ) -> RetrievalResult:
        catalog = self.load_catalog(db_path)
        query = f"{question} {sql_prefix}".strip()
        limit = max(1, top_k or self.top_tables)
        table_scores = self._score_tables(query, catalog)
        ranked_tables = sorted(table_scores.items(), key=lambda item: (-item[1], item[0]))
        # A fixed top-k otherwise injects unrelated zero-score tables into small
        # databases. Keep at least the best table, then only genuine matches.
        best_score = ranked_tables[0][1]
        relevance_floor = best_score * 0.2
        matched_tables = [
            name for name, score in ranked_tables if score > 0 and score >= relevance_floor
        ]
        # Expand weak/ambiguous retrieval instead of hiding the correct table
        # behind a fixed top-k. Strong matches remain compact.
        weak_retrieval = best_score < 0.35
        if weak_retrieval:
            limit = min(len(ranked_tables), max(limit, 6))
        elif len(matched_tables) > limit and ranked_tables[limit][1] >= best_score * 0.72:
            limit = min(len(ranked_tables), max(limit, 4))
        candidates = [name for name, _ in ranked_tables] if weak_retrieval else matched_tables
        initial_tables = (candidates or [ranked_tables[0][0]])[:limit]

        fk_paths, bridge_tables = self._complete_fk_paths(initial_tables, catalog)
        selected_tables = list(dict.fromkeys(initial_tables + bridge_tables))
        selected_columns = self._rerank_columns(query, selected_tables, catalog)

        # JOIN keys and primary keys must survive field-level reranking.
        for table_name in selected_tables:
            required = {
                column.name
                for column in catalog.tables[table_name].columns
                if column.primary_key
            }
            for fk in catalog.foreign_keys:
                if fk.source_table == table_name:
                    required.add(fk.source_column)
                if fk.target_table == table_name:
                    required.add(fk.target_column)
            selected_columns[table_name] = list(
                dict.fromkeys(selected_columns.get(table_name, []) + sorted(required))
            )

        grounded_values = self._ground_values(db_path, query, selected_columns, catalog)
        context = self._render_context(
            selected_tables,
            selected_columns,
            fk_paths,
            grounded_values,
            catalog,
        )
        return RetrievalResult(
            context=context,
            tables=selected_tables,
            reason=reason,
            initial_tables=initial_tables,
            bridge_tables=bridge_tables,
            columns=selected_columns,
            foreign_key_paths=fk_paths,
            grounded_values=grounded_values,
            scores={name: round(table_scores[name], 6) for name in initial_tables},
        )

    def load_catalog(self, db_path: str, refresh: bool = False) -> SchemaCatalog:
        cache_key = str(Path(db_path).resolve())
        if cache_key in self._catalog_cache and not refresh:
            return self._catalog_cache[cache_key]
        metadata = self._load_metadata()
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            table_names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            tables: dict[str, TableInfo] = {}
            foreign_keys: list[ForeignKeyInfo] = []
            for table_name in table_names:
                table_meta = metadata.get("tables", {}).get(table_name, {})
                raw_column_meta = table_meta.get("columns", {})
                column_meta = {
                    name: value.get("description", "") if isinstance(value, dict) else value
                    for name, value in raw_column_meta.items()
                }
                raw_columns = conn.execute(f"PRAGMA table_info({_quote(table_name)})").fetchall()
                columns = [
                    ColumnInfo(
                        name=str(row[1]),
                        data_type=str(row[2] or "unknown"),
                        description=str(column_meta.get(str(row[1]), "")),
                        primary_key=bool(row[5]),
                    )
                    for row in raw_columns
                ]
                tables[table_name] = TableInfo(
                    name=table_name,
                    description=str(table_meta.get("description", "")),
                    columns=columns,
                )
                for fk in conn.execute(f"PRAGMA foreign_key_list({_quote(table_name)})").fetchall():
                    foreign_keys.append(
                        ForeignKeyInfo(
                            source_table=table_name,
                            source_column=str(fk[3]),
                            target_table=str(fk[2]),
                            target_column=str(fk[4]),
                        )
                    )
            for relation in metadata.get("inferred_relations", []):
                if float(relation.get("confidence", 0)) < 0.75:
                    continue
                candidate = ForeignKeyInfo(
                    source_table=str(relation.get("source_table", "")),
                    source_column=str(relation.get("source_column", "")),
                    target_table=str(relation.get("target_table", "")),
                    target_column=str(relation.get("target_column", "")),
                )
                if candidate.source_table in tables and candidate.target_table in tables and candidate not in foreign_keys:
                    foreign_keys.append(candidate)
            catalog = SchemaCatalog(tables=tables, foreign_keys=foreign_keys)
            self._catalog_cache[cache_key] = catalog
            return catalog
        finally:
            conn.close()

    def _load_metadata(self) -> dict[str, Any]:
        if self.semantic_catalog is not None:
            return self.semantic_catalog
        if not self.metadata_path.exists():
            return {}
        try:
            return json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _score_tables(self, query: str, catalog: SchemaCatalog) -> dict[str, float]:
        documents = {name: table.document() for name, table in catalog.tables.items()}
        tokenized = {name: _tokens(document) for name, document in documents.items()}
        query_tokens = _tokens(query)
        document_count = max(1, len(documents))
        avg_length = sum(len(tokens) for tokens in tokenized.values()) / document_count
        document_frequency = Counter()
        for tokens in tokenized.values():
            document_frequency.update(set(tokens))

        query_vector = _char_ngrams(query)
        lexical_scores: dict[str, float] = {}
        character_scores: dict[str, float] = {}
        exact_scores: dict[str, float] = {}
        for name, tokens in tokenized.items():
            counts = Counter(tokens)
            bm25 = 0.0
            for token in query_tokens:
                if not counts[token]:
                    continue
                df = document_frequency[token]
                idf = math.log(1 + (document_count - df + 0.5) / (df + 0.5))
                frequency = counts[token]
                denominator = frequency + 1.5 * (1 - 0.75 + 0.75 * len(tokens) / max(avg_length, 1))
                bm25 += idf * frequency * 2.5 / denominator
            lexical_scores[name] = bm25
            character_scores[name] = _cosine(query_vector, _char_ngrams(documents[name]))
            exact_scores[name] = 1.0 if name.lower() in query.lower() else 0.0
        if not self.embedder:
            return {name: self.lexical_weight * lexical_scores[name]
                    + self.vector_weight * character_scores[name] + exact_scores[name]
                    for name in documents}
        names = sorted(documents)
        cache_key = hashlib.sha256(
            "\n".join(f"{name}:{documents[name]}" for name in names).encode()
        ).hexdigest()
        cached = self._embedding_cache.get(cache_key)
        if cached is None:
            matrix = self.embedder.embed([documents[name] for name in names])
            self._embedding_cache = {cache_key: (names, matrix)}
        else:
            names, matrix = cached
        query_embedding = self.embedder.embed([query])[0]
        dense_scores = {name: max(0.0, float(matrix[index] @ query_embedding))
                        for index, name in enumerate(names)}
        max_lexical = max(lexical_scores.values(), default=1.0) or 1.0
        sparse_weight = 1.0 - self.embedding_weight
        return {
            name: sparse_weight * (
                0.72 * lexical_scores[name] / max_lexical
                + 0.28 * character_scores[name]
            ) + self.embedding_weight * dense_scores[name] + exact_scores[name]
            for name in documents
        }

    def _rerank_columns(
        self,
        query: str,
        selected_tables: list[str],
        catalog: SchemaCatalog,
    ) -> dict[str, list[str]]:
        query_tokens = Counter(_tokens(query))
        query_vector = _char_ngrams(query)
        result: dict[str, list[str]] = {}
        for table_name in selected_tables:
            scored = []
            for column in catalog.tables[table_name].columns:
                document = column.document(table_name)
                lexical = sum(query_tokens[token] for token in set(_tokens(document)))
                vector = _cosine(query_vector, _char_ngrams(document))
                exact = 2.0 if column.name.lower() in query.lower() else 0.0
                scored.append((lexical + vector + exact, column.name))
            result[table_name] = [name for _, name in sorted(scored, key=lambda item: (-item[0], item[1]))[: self.top_columns]]
        return result

    def _complete_fk_paths(
        self,
        selected_tables: list[str],
        catalog: SchemaCatalog,
    ) -> tuple[list[list[str]], list[str]]:
        graph: dict[str, list[str]] = {name: [] for name in catalog.tables}
        for fk in catalog.foreign_keys:
            graph.setdefault(fk.source_table, []).append(fk.target_table)
            graph.setdefault(fk.target_table, []).append(fk.source_table)

        paths: list[list[str]] = []
        bridges: list[str] = []
        for start, end in combinations(selected_tables, 2):
            path = self._shortest_path(graph, start, end)
            if path:
                paths.append(path)
                for table in path[1:-1]:
                    if table not in selected_tables and table not in bridges:
                        bridges.append(table)
        return paths, bridges

    @staticmethod
    def _shortest_path(graph: dict[str, list[str]], start: str, end: str) -> list[str]:
        queue = deque([(start, [start])])
        visited = {start}
        while queue:
            node, path = queue.popleft()
            if node == end:
                return path
            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))
        return []

    def _ground_values(
        self,
        db_path: str,
        query: str,
        selected_columns: dict[str, list[str]],
        catalog: SchemaCatalog,
    ) -> dict[str, list[str]]:
        if self.max_value_samples == 0:
            return {}
        metadata = self._load_metadata()
        grounded: dict[str, list[str]] = {}
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            for table_name, column_names in selected_columns.items():
                table = catalog.tables[table_name]
                table_metadata = metadata.get("tables", {}).get(table_name, {})
                allowed_columns = set(table_metadata.get("value_grounding_columns", []))
                type_map = {column.name: column.data_type.upper() for column in table.columns}
                for column_name in column_names:
                    # Real values can contain PII or secrets. Sampling is opt-in
                    # per column instead of being enabled for every text field.
                    if column_name not in allowed_columns:
                        continue
                    if not any(kind in type_map.get(column_name, "") for kind in ("CHAR", "TEXT", "DATE", "BOOL")):
                        continue
                    values = self._sample_values(conn, table_name, column_name)
                    mappings = (
                        table_metadata
                        .get("value_descriptions", {})
                        .get(column_name, {})
                    )
                    rendered = [
                        f"{value} ({mappings[value]})" if value in mappings else value
                        for value in values
                    ]
                    if rendered:
                        grounded[f"{table_name}.{column_name}"] = rendered
            return grounded
        finally:
            conn.close()

    def _sample_values(self, conn: sqlite3.Connection, table: str, column: str) -> list[str]:
        try:
            rows = conn.execute(
                f"SELECT DISTINCT {_quote(column)} FROM {_quote(table)} "
                f"WHERE {_quote(column)} IS NOT NULL LIMIT {self.max_value_samples}"
            ).fetchall()
            return [repr(str(row[0])[:60]) for row in rows]
        except sqlite3.Error:
            return []

    @staticmethod
    def _render_context(
        selected_tables: list[str],
        selected_columns: dict[str, list[str]],
        fk_paths: list[list[str]],
        grounded_values: dict[str, list[str]],
        catalog: SchemaCatalog,
    ) -> str:
        lines = ["通过 Schema RAG 检索到的数据库知识："]
        for table_name in selected_tables:
            table = catalog.tables[table_name]
            description = f" -- {table.description}" if table.description else ""
            lines.append(f"表 `{table_name}`{description}:")
            column_map = {column.name: column for column in table.columns}
            for column_name in selected_columns.get(table_name, []):
                column = column_map[column_name]
                flags = " PRIMARY KEY" if column.primary_key else ""
                hint = f" -- {column.description}" if column.description else ""
                lines.append(f"  - `{column.name}` {column.data_type}{flags}{hint}")

        selected_set = set(selected_tables)
        relevant_fks = [
            fk
            for fk in catalog.foreign_keys
            if fk.source_table in selected_set and fk.target_table in selected_set
        ]
        if relevant_fks:
            lines.append("外键关系:")
            for fk in relevant_fks:
                lines.append(
                    f"  - `{fk.source_table}`.`{fk.source_column}` = "
                    f"`{fk.target_table}`.`{fk.target_column}`"
                )
        if fk_paths:
            lines.append("推荐 JOIN 路径:")
            lines.extend(f"  - {' -> '.join(path)}" for path in fk_paths)
        if grounded_values:
            lines.append("字段真实取值样例（仅用于过滤值对齐）:")
            for qualified_column, values in grounded_values.items():
                lines.append(f"  - `{qualified_column}`: {', '.join(values)}")
        return "\n".join(lines)
