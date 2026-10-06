"""Independent expectations for the bounded synthetic model proof."""

import unittest

from scripts.cloud_semantic_smoke import cases, check_result, build_automatic_prompt


class CloudProofContractTests(unittest.TestCase):
    def test_production_prompt_classifies_before_conditional_extraction(self):
        prompt = build_automatic_prompt({"kind": "document", "semantic_summary": {}},
            {"url": "https://example.invalid/source", "excerpt": "Synthetic evidence."})
        self.assertIn("decide the outcome BEFORE extracting a candidate", prompt)
        self.assertIn("ONLY if the outcome is CANDIDATES_PROPOSED", prompt)
        self.assertIn("Otherwise candidates must be empty", prompt)
        self.assertIn("do not select one side", prompt)
        self.assertIn("same evidence rules in every language", prompt)

    def test_holdouts_include_bilingual_noise_and_conflict(self):
        by_id = {case["id"]: case for case in cases()}
        self.assertEqual(len(by_id), 10)
        for name in ("irrelevant_pt", "incidental_sequence", "conflicting_pt"):
            self.assertNotIn("CANDIDATES_PROPOSED", by_id[name]["outcomes"])
        self.assertEqual(by_id["unfamiliar_tool"]["title"], "CopperRelay")

    def test_missing_or_invalid_model_output_cannot_pass(self):
        self.assertEqual(check_result(cases()[0], None, ["invalid"]), ["model_contract_invalid"])

    def test_irrelevant_content_cannot_create_a_candidate(self):
        plan = {"outcome": "CANDIDATES_PROPOSED", "candidates": [{"title": "ImaginaryTool"}]}
        self.assertIn("unsafe_candidate", check_result(cases()[2], plan, []))

    def test_useful_content_must_preserve_the_source_subject(self):
        plan = {"outcome": "CANDIDATES_PROPOSED", "candidates": [{"title": "InventedTool"}]}
        self.assertIn("subject_not_preserved", check_result(cases()[0], plan, []))

    def test_unsupported_claim_is_a_quality_failure(self):
        plan = {"outcome": "CANDIDATES_PROPOSED", "candidates": [
            {"title": "TaskWeave", "claims": ["TaskWeave guarantees PostgreSQL performance."]}]}
        self.assertIn("unsupported_claim", check_result(cases()[5], plan, []))


if __name__ == "__main__":
    unittest.main()
