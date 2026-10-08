"""Acceptance-policy regressions: daily operation, not a mandatory bulk dataset."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from project_alignment_guard import validate

COVERAGE = [
    "scheduled_empty_noop", "scheduled_single_useful_item", "mixed_small_batches",
    "duplicates_and_noise", "weak_and_conflicting_evidence", "malformed_or_unsupported_input",
    "automatic_canonical_index_and_retrieval", "ordinary_interruption_resume",
    "privacy_and_zero_cost",
]


class DailyAcceptanceAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tl-contract-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "docs").mkdir()
        for path in ("docs/PROJECT_CHARTER.md", "docs/PROJECT_CONTRACT.json",
                     "docs/PROJECT_ROADMAP.md", "AGENTS.md"):
            shutil.copyfile(ROOT / path, self.root / path)
        self.contract_path = self.root / "docs/PROJECT_CONTRACT.json"
        self.contract = json.loads(self.contract_path.read_text())
        self.contract["charter_version"] = "1.1"
        self.contract["completion_requires"] = [
            entry for entry in self.contract["completion_requires"]
            if entry != "500-item mixed-backlog black-box acceptance pass"
        ]
        requirement = "representative daily-flow end-to-end acceptance pass"
        if requirement not in self.contract["completion_requires"]:
            self.contract["completion_requires"].append(requirement)
        self.contract["acceptance_policy"] = {
            "mode": "representative_daily_flow", "minimum_items_to_run": 0,
            "user_supplied_bulk_dataset_required": False, "stress_test_required": False,
            "required_scenarios": COVERAGE.copy(),
        }
        for path in ("docs/PROJECT_CHARTER.md", "docs/PROJECT_ROADMAP.md"):
            destination = self.root / path
            destination.write_text(destination.read_text().replace("CHARTER_VERSION: 1.0", "CHARTER_VERSION: 1.1"))

    def check(self):
        self.contract_path.write_text(json.dumps(self.contract))
        return validate(self.root)

    def test_representative_daily_acceptance_needs_no_500_item_dataset(self):
        self.assertEqual(self.check(), [])

    def test_bulk_dataset_requirement_is_rejected(self):
        self.contract["acceptance_policy"]["user_supplied_bulk_dataset_required"] = True
        self.assertIn("acceptance_policy_user_supplied_bulk_dataset_required", self.check())

    def test_waiting_for_input_volume_is_rejected(self):
        self.contract["acceptance_policy"]["minimum_items_to_run"] = 500
        self.assertIn("acceptance_policy_minimum_items_to_run", self.check())

    def test_stress_test_cannot_become_release_requirement(self):
        self.contract["acceptance_policy"]["stress_test_required"] = True
        self.assertIn("acceptance_policy_stress_test_required", self.check())

    def test_acceptance_must_cover_recovery(self):
        self.contract["acceptance_policy"]["required_scenarios"].remove("ordinary_interruption_resume")
        self.assertIn("acceptance_policy_scenarios_missing", self.check())

    def test_green_internal_tests_cannot_replace_end_to_end_acceptance(self):
        self.contract["project_status"] = "COMPLETE"
        self.contract["black_box_acceptance_status"] = "NOT_RUN"
        self.assertIn("complete_without_black_box_pass", self.check())

    def test_old_bulk_completion_gate_is_rejected(self):
        self.contract["completion_requires"].append("500-item mixed-backlog black-box acceptance pass")
        self.assertIn("contract_obsolete_bulk_acceptance_requirement", self.check())


if __name__ == "__main__":
    unittest.main()
