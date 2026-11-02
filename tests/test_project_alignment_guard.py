"""The approved V1 acceptance revision cannot silently weaken completion."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from project_alignment_guard import validate

ROOT = Path(__file__).resolve().parents[1]


class AlignmentAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "docs").mkdir()
        for name in ("PROJECT_CHARTER.md", "PROJECT_CONTRACT.json", "PROJECT_ROADMAP.md"):
            shutil.copyfile(ROOT / "docs" / name, self.root / "docs" / name)
        shutil.copyfile(ROOT / "AGENTS.md", self.root / "AGENTS.md")
        self.contract_path = self.root / "docs" / "PROJECT_CONTRACT.json"
        self.contract = json.loads(self.contract_path.read_text())

    def check(self):
        self.contract_path.write_text(json.dumps(self.contract))
        return validate(self.root)

    def test_current_metadata_is_aligned_without_claiming_acceptance(self):
        self.assertEqual(self.contract["charter_version"], "1.1")
        self.assertEqual(self.contract["black_box_acceptance_status"], "NOT_RUN")
        self.assertEqual(self.check(), [])

    def test_removed_scale_requirement_needs_explicit_approved_revision(self):
        self.contract["scope_revision"]["owner_approval"] = ""
        self.assertIn("scope_revision_owner_approval_missing", self.check())

    def test_every_original_operational_case_remains_required(self):
        for case in self.contract["v1_acceptance"]["required_cases"][:]:
            with self.subTest(case=case):
                original = self.contract["v1_acceptance"]["required_cases"][:]
                self.contract["v1_acceptance"]["required_cases"].remove(case)
                self.assertIn("v1_acceptance_required_cases_mismatch", self.check())
                self.contract["v1_acceptance"]["required_cases"] = original

    def test_synthetic_days_or_short_credential_age_cannot_replace_real_acceptance(self):
        self.contract["v1_acceptance"]["real_execution_required"] = False
        self.contract["v1_acceptance"]["consecutive_days"] = 1
        self.contract["v1_acceptance"]["drive_credential_age_days_exclusive_minimum"] = 7
        errors = self.check()
        for name in ("real_execution_required", "consecutive_days", "drive_credential_age_days_exclusive_minimum"):
            self.assertIn("v1_acceptance_" + name, errors)

    def test_pass_without_remote_acceptance_evidence_is_rejected(self):
        self.contract["black_box_acceptance_status"] = "PASS"
        self.assertIn("acceptance_evidence_missing", self.check())

    def proof(self):
        return {
            "real_execution": True,
            "scheduled_days_utc": [f"2026-11-{day:02d}" for day in range(1, 8)],
            "cases_passed": self.contract["v1_acceptance"]["required_cases"][:],
            "drive_credential_age_days_without_manual_renewal": 9,
            "private_evidence_manifest_sha256": "a" * 64,
        }

    def test_sanitized_real_acceptance_summary_can_satisfy_metadata_gate(self):
        self.contract["black_box_acceptance_status"] = "PASS"
        self.contract["acceptance_evidence"] = self.proof()
        self.assertEqual(self.check(), [])

    def test_nonconsecutive_days_missing_cases_and_eight_day_token_are_rejected(self):
        self.contract["black_box_acceptance_status"] = "PASS"
        self.contract["acceptance_evidence"] = self.proof()
        self.contract["acceptance_evidence"]["scheduled_days_utc"][-1] = "2026-11-09"
        self.contract["acceptance_evidence"]["cases_passed"].pop()
        self.contract["acceptance_evidence"]["drive_credential_age_days_without_manual_renewal"] = 8
        errors = self.check()
        for code in ("acceptance_days_not_consecutive", "acceptance_cases_incomplete", "acceptance_credential_age_insufficient"):
            self.assertIn(code, errors)

    def test_synthetic_proof_or_invalid_manifest_digest_cannot_pass(self):
        self.contract["black_box_acceptance_status"] = "PASS"
        self.contract["acceptance_evidence"] = self.proof()
        self.contract["acceptance_evidence"]["real_execution"] = False
        self.contract["acceptance_evidence"]["private_evidence_manifest_sha256"] = "not-a-digest"
        errors = self.check()
        self.assertIn("acceptance_not_real_execution", errors)
        self.assertIn("acceptance_evidence_digest_invalid", errors)

    def test_complete_still_requires_black_box_pass(self):
        self.contract["project_status"] = "COMPLETE"
        self.assertIn("complete_without_black_box_pass", self.check())

    def test_human_approval_or_cost_drift_still_fails(self):
        self.contract["hard_constraints"]["manual_canonical_approval_required"] = True
        self.contract["hard_constraints"]["mandatory_additional_recurring_cost_brl"] = 1
        errors = self.check()
        self.assertIn("hard_constraint_manual_canonical_approval_required", errors)
        self.assertIn("hard_constraint_mandatory_additional_recurring_cost_brl", errors)


if __name__ == "__main__":
    unittest.main()
