from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from Demo_db import build_demo_db
from Graph import build_graph
from Llm_client import MockLLMClient
from Retrieval import SQLiteSchemaRetriever
from Telemetry import configure_telemetry


class TelemetryTest(unittest.TestCase):
    def test_agent_spans_are_nested_and_do_not_capture_content(self):
        exporter = InMemorySpanExporter()
        settings = SimpleNamespace(
            tracing_enabled=True,
            tracing_exporter="none",
            otel_service_name="verisql-test",
            otel_environment="test",
            otel_sample_ratio=1.0,
        )
        runtime = configure_telemetry(settings, exporter_override=exporter)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = build_demo_db(Path(temp_dir) / "demo.sqlite")
            graph = build_graph(
                llm_client=MockLLMClient(),
                schema_retriever=SQLiteSchemaRetriever(top_tables=2),
                initial_schema_rag=True,
            )
            state = {
                "question": "各部门的平均薪资是多少？",
                "db_path": str(db_path),
                "retry_count": 0,
                "max_retries": 3,
                "error_history": [],
                "retrieval_events": [],
                "conversation_history": [],
            }
            with trace.get_tracer("test").start_as_current_span("verisql.query.test"):
                result = graph.invoke(state)
            self.assertEqual("success", result["status"])

        runtime.provider.force_flush()
        spans = exporter.get_finished_spans()
        names = {span.name for span in spans}
        self.assertIn("verisql.schema.retrieve", names)
        self.assertIn("verisql.llm.generate_sql", names)
        self.assertIn("verisql.sql.validate", names)
        self.assertIn("verisql.sql.execute", names)
        self.assertIn("verisql.answer.generate", names)
        root = next(span for span in spans if span.name == "verisql.query.test")
        children = [span for span in spans if span.parent and span.parent.span_id == root.context.span_id]
        self.assertTrue(children)

        serialized_attributes = " ".join(
            str(value)
            for span in spans
            for value in (span.attributes or {}).values()
        )
        self.assertNotIn("SELECT ", serialized_attributes)
        self.assertNotIn("各部门的平均薪资", serialized_attributes)
        self.assertNotIn("Alice", serialized_attributes)


if __name__ == "__main__":
    unittest.main()
