from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from curation_decision_apply import parse_decisions, read_json_file


class CurationDecisionExampleTests(unittest.TestCase):
    def test_tracked_example_is_empty_and_valid(self) -> None:
        payload = read_json_file(ROOT / "examples/curation-decisions.example.json")
        self.assertEqual([], parse_decisions(payload))

    def test_parser_still_rejects_private_extra_fields(self) -> None:
        payload = {
            "schema_version": 1,
            "decisions_version": "0.1.0",
            "items": [
                {
                    "package_id": "0" * 20,
                    "revision_key": "1" * 20,
                    "state": "rejected",
                    "source_name": "must-not-enter-the-decision-boundary",
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "decision_fields"):
            parse_decisions(payload)


if __name__ == "__main__":
    unittest.main()
