"""Synthetic-only E3 end-to-end transaction commit and terminal sidecar."""
from __future__ import annotations

import hashlib
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from candidate_production_coordinator import read_state
from e1_lote_prepare import LOTES, encode, past_state
from e3_commit_fixture import (
    CommitFixtureError, exercise_commit, resume_terminal_commit,
)
from e3_coordinator_fixture import resume_fixture
from e3_lote_state import related_paths, verify_settlement
from e3_recovery_fixture import MARKER, MARKER_BYTES, load_journal
from tests.test_e3_transaction_preflight import E3PreflightTests


class E3CommittedFixtureTests(unittest.TestCase):
    def setUp(self):
        case = E3PreflightTests("test_no_write_decision_does_not_touch_canonical")
        case.setUp()
        self.addCleanup(case.tmp.cleanup)
        self.case = case
        self.root = case.root
        case.write(MARKER, MARKER_BYTES)
        self.git = self.root / "coord.git"
        subprocess.run(["git", "init", "--bare", str(self.git)], check=True, capture_output=True)
        self.raw_lot = case.lote_raw
        self.relative = case.path
        self.decisions = case.decision_bytes(case.create_row())
        self.target = "00_LIBRARY/02_WEB_DEVELOPMENT/UI_COMPONENTS/technology-pixel-rig-tool.md"
        self.original_master = (self.root / "00_LIBRARY/MASTER_INDEX.md").read_bytes()

    def state(self):
        _, state, errors = read_state(str(self.git))
        self.assertEqual(errors, [])
        return state

    def terminal_raw(self):
        _, receipt = related_paths(self.relative)
        return (self.root / LOTES / receipt).read_bytes()

    def test_create_commits_and_settles_without_duplicate_processing(self):
        state, tx = exercise_commit(self.root, self.git, self.raw_lot, self.relative, self.decisions)
        self.assertEqual(state, "COMMITTED_RELEASED")
        self.assertTrue((self.root / self.target).exists())
        self.assertNotEqual((self.root / "00_LIBRARY/MASTER_INDEX.md").read_bytes(), self.original_master)
        self.assertTrue(verify_settlement(self.raw_lot, self.relative, self.decisions, self.terminal_raw())["settled"])
        self.assertEqual(self.state()["state"], "FREE")
        self.assertEqual(self.state()["release_reason"], "VERIFIED_COMMITTED")
        journal = load_journal(self.root, tx)
        self.assertEqual(journal["state"], "COMMITTED")
        self.assertEqual(resume_terminal_commit(self.root, self.git, self.raw_lot, self.relative, self.decisions),
                         "ALREADY_COMMITTED_RELEASED")

    def test_index_write_failure_preserves_active_lease_until_recovery(self):
        state, tx = exercise_commit(self.root, self.git, self.raw_lot, self.relative,
                                    self.decisions, fail_on_write=2)
        self.assertEqual(state, "RECOVERY_REQUIRED_OWNED")
        self.assertEqual(self.state()["state"], "ACTIVE")
        self.assertTrue((self.root / self.target).exists())
        self.assertEqual(resume_fixture(self.root, self.git, tx, self.case.lote["lote_id"]),
                         "RECOVERED_RELEASED")
        self.assertFalse((self.root / self.target).exists())
        self.assertEqual((self.root / "00_LIBRARY/MASTER_INDEX.md").read_bytes(), self.original_master)
        self.assertEqual(self.state()["state"], "FREE")

    def test_existing_terminal_sidecar_disallows_second_mutation(self):
        exercise_commit(self.root, self.git, self.raw_lot, self.relative, self.decisions)
        with self.assertRaises(Exception):
            exercise_commit(self.root, self.git, self.raw_lot, self.relative, self.decisions)
        self.assertEqual(self.state()["state"], "FREE")

    def test_no_write_hold_settles_without_git_owner(self):
        hold = self.case.decision_bytes(self.case.decision())
        state, tx = exercise_commit(self.root, self.git, self.raw_lot, self.relative, hold)
        self.assertEqual((state, tx), ("NO_WRITE_SETTLED", None))
        self.assertEqual(verify_settlement(self.raw_lot, self.relative, hold, self.terminal_raw())["writes"], 0)
        self.assertEqual((self.root / "00_LIBRARY/MASTER_INDEX.md").read_bytes(), self.original_master)

    def test_reference_update_retains_status_in_committed_canonical(self):
        from library_index_build import validate_record, planned_indexes
        prior = self.case.original.replace(b"STATUS: TEST", b"STATUS: REFERENCE")
        self.case.write(self.case.canonical_path, prior)
        for p, data in planned_indexes([validate_record(self.case.canonical_path, prior)]).items():
            self.case.write(p, data)
        self.original_master = (self.root / "00_LIBRARY/MASTER_INDEX.md").read_bytes()
        self.case.item["evidence_compact"]["evidence"]["samples"] = [
            "React Bits provides animated React components. A standalone export workflow is now supported."
        ]
        self.case.item["matches"] = [{"record_id": "0123456789abcdef0123", "title": "React Bits"}]
        self.case.lote["items_sha256"] = hashlib.sha256(encode(self.case.lote["itens"])).hexdigest()
        self.case.lote_raw = encode(self.case.lote)
        self.raw_lot = self.case.lote_raw
        proposal = prior.decode().replace("STATUS: REFERENCE", "STATUS: TEST")
        choice = self.case.decision(
            "UPDATE", motivo=None, confianca=0.95, record_type="TECHNOLOGY",
            route="02_WEB_DEVELOPMENT/UI_COMPONENTS", target_record_id="0123456789abcdef0123",
            record_markdown=proposal, claims=[
                {"text": "Standalone export workflow supported",
                 "evidence_quote": "standalone export workflow"}])
        self.decisions = self.case.decision_bytes(choice)
        state, tx = exercise_commit(self.root, self.git, self.raw_lot, self.relative, self.decisions)
        self.assertEqual(state, "COMMITTED_RELEASED")
        after = (self.root / self.case.canonical_path).read_bytes()
        self.assertIn(b"STATUS: REFERENCE", after)
        self.assertTrue(after.startswith(prior.rstrip(b"\n")))
        self.assertEqual((self.root / "00_LIBRARY/MASTER_INDEX.md").read_bytes(), self.original_master)
        self.assertEqual(verify_settlement(self.raw_lot, self.relative, self.decisions, self.terminal_raw())["writes"], 1)


if __name__ == "__main__":
    unittest.main()
