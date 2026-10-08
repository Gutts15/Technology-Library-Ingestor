"""The startup self-test must not use benchmark facts or accept candidates."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pinned_cpu_prepare import prepare


class PinnedPreparationTests(unittest.TestCase):
    def test_empty_synthetic_self_test_uses_pinned_transport_without_benchmark_content(self):
        with patch("pinned_cpu_prepare.infer", return_value={
                "outcome": "NO_REUSABLE_KNOWLEDGE", "candidates": []}) as model:
            self.assertTrue(prepare(17))
        prompt, schema, timeout = model.call_args.args
        self.assertEqual(timeout, 17)
        self.assertIn('"samples":[]', prompt)
        self.assertIn("https://example.invalid/model-readiness", prompt)
        for subject in ("TaskWeave", "NuvemConta", "CopperRelay"):
            self.assertNotIn(subject, prompt)
        self.assertEqual(schema["type"], "object")

    def test_candidate_or_missing_response_cannot_establish_readiness(self):
        for response in (None, {}, {"outcome": "CANDIDATES_PROPOSED", "candidates": []},
                         {"outcome": "NEEDS_REVIEW", "candidates": [{"title": "Invented"}]},
                         {"outcome": "SUSPECTED_ACCIDENTAL", "candidates": []}):
            with self.subTest(response=response), patch("pinned_cpu_prepare.infer", return_value=response):
                self.assertFalse(prepare())

    def test_held_empty_input_is_a_valid_startup_result(self):
        with patch("pinned_cpu_prepare.infer", return_value={"outcome": "NEEDS_REVIEW", "candidates": []}):
            self.assertTrue(prepare())


if __name__ == "__main__":
    unittest.main()
