from __future__ import annotations

import json
import unittest

from Web_app import NODE_LABELS, encode_sse


class SSETest(unittest.TestCase):
    def test_event_is_valid_single_data_line_json(self):
        encoded = encode_sse(
            "progress",
            {"message": "第一行\nevent: injected", "sequence": 1},
        )
        self.assertTrue(encoded.startswith("event: progress\ndata: "))
        self.assertTrue(encoded.endswith("\n\n"))
        self.assertEqual(1, sum(line.startswith("data:") for line in encoded.splitlines()))
        payload = encoded.splitlines()[1].removeprefix("data: ")
        self.assertEqual("第一行\nevent: injected", json.loads(payload)["message"])

    def test_all_user_visible_workflow_nodes_have_labels(self):
        expected = {
            "get_schema_context",
            "generate_sql",
            "dynamic_retrieve",
            "corrective_retrieve",
            "validate_sql",
            "check_cost",
            "execute_sql",
            "reflect",
            "generate_answer",
            "human_handoff",
            "cancel_query",
        }
        self.assertEqual(expected, set(NODE_LABELS))

    def test_progress_timing_fields_are_sse_safe(self):
        encoded = encode_sse(
            "progress",
            {
                "sequence": 2,
                "node": "generate_sql",
                "message": NODE_LABELS["generate_sql"],
                "duration_ms": 1450,
                "status": "completed",
            },
        )
        payload = json.loads(encoded.splitlines()[1].removeprefix("data: "))
        self.assertEqual(1450, payload["duration_ms"])
        self.assertEqual("completed", payload["status"])


if __name__ == "__main__":
    unittest.main()
