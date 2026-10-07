import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from evidence_subject_selection import extraction_prompt, validate_selection


class SubjectSelectionTests(unittest.TestCase):
    def test_verbatim_bilingual_technical_capability(self):
        for source, subject in [("TaskWeave schedules dependent tasks.", "TaskWeave"),
                                ("FilaTrilha organiza tarefas dependentes.", "FilaTrilha")]:
            value = {"decision": "TECHNICAL", "subject": subject, "evidence_quote": source}
            self.assertEqual(validate_selection(value, source), (value, None))

    def test_invented_quote_or_subject_cannot_pass(self):
        source = "TaskWeave schedules dependent tasks."
        for subject, quote in [("InventedTool", source),
                               ("TaskWeave", "TaskWeave guarantees uptime.")]:
            self.assertEqual(validate_selection({"decision": "TECHNICAL", "subject": subject,
                "evidence_quote": quote}, source)[1], "selection_unbound_evidence")

    def test_quote_without_subject_cannot_pass(self):
        source = "TaskWeave is a tool. It schedules tasks."
        self.assertIsNotNone(validate_selection({"decision": "TECHNICAL", "subject": "TaskWeave",
            "evidence_quote": "It schedules tasks."}, source)[1])

    def test_nontechnical_and_conflict_cannot_carry_candidate_content(self):
        for decision in ("NON_TECHNICAL", "CONFLICT", "INSUFFICIENT"):
            self.assertIsNone(validate_selection({"decision": decision, "subject": "",
                "evidence_quote": ""}, "Synthetic data.")[1])
            self.assertIsNotNone(validate_selection({"decision": decision, "subject": "MadeUp",
                "evidence_quote": ""}, "Synthetic data.")[1])

    def test_malformed_fields_and_control_characters_fail_closed(self):
        self.assertIsNotNone(validate_selection({"decision": "TECHNICAL"}, "data")[1])
        self.assertIsNotNone(validate_selection({"decision": "TECHNICAL", "subject": "Tool\n",
            "evidence_quote": "Tool\n schedules tasks."}, "Tool\n schedules tasks.")[1])

    def test_nontechnical_cannot_enter_extraction(self):
        with self.assertRaises(ValueError):
            extraction_prompt({"decision": "NON_TECHNICAL"}, "data")


if __name__ == "__main__":
    unittest.main()
