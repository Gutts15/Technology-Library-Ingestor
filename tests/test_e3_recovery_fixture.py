"""E3 recovery fixture tests. Synthetic-only files; never a real Drive remote."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import e3_recovery_fixture as recovery
from tests.test_e3_transaction_preflight import E3PreflightTests


class TestE3FixtureRecovery(unittest.TestCase):
    def setUp(self):
        self.case = E3PreflightTests("test_no_write_decision_does_not_touch_canonical")
        self.case.setUp()
        self.addCleanup(self.case.tmp.cleanup)
        self.root = self.case.root
        self.case.write(recovery.MARKER, recovery.MARKER_BYTES)
        self.row = self.case.create_row()
        self.decisions = self.case.decision_bytes(self.row)
        self.raw_lot = self.case.lote_raw
        self.path = self.case.path
        self.new_record = "00_LIBRARY/02_WEB_DEVELOPMENT/UI_COMPONENTS/technology-pixel-rig-tool.md"

    def exercise(self, **kwargs):
        return recovery.exercise_fixture(
            self.root, self.raw_lot, self.path, self.decisions, **kwargs)

    def test_successful_local_mutation_then_verified_rollback(self):
        state, tx = self.exercise()
        self.assertEqual(state, "RECOVERED")
        self.assertIsNotNone(tx)
        self.assertFalse((self.root / self.new_record).exists())
        self.assertEqual((self.root / self.case.canonical_path).read_bytes(), self.case.original)
        self.assertEqual((self.root / "00_LIBRARY/MASTER_INDEX.md").read_bytes(),
                         self.case.initial_master)
        journal = recovery.load_journal(self.root, tx)
        self.assertTrue(journal["rollback_verified"])
        self.assertTrue(journal["snapshots_verified"])
        self.assertEqual(recovery.recover_fixture(self.root, tx), "ALREADY_RECOVERED")

    def test_write_failure_requires_recovery_next_session(self):
        state, tx = self.exercise(fail_on_write=2)
        self.assertEqual(state, "RECOVERY_REQUIRED")
        self.assertTrue((self.root / self.new_record).exists())
        self.assertEqual(recovery.recover_fixture(self.root, tx), "RECOVERED")
        self.assertFalse((self.root / self.new_record).exists())
        self.assertEqual((self.root / "00_LIBRARY/MASTER_INDEX.md").read_bytes(),
                         self.case.initial_master)

    def test_very_first_write_failure_reconciles_to_clean(self):
        state, tx = self.exercise(fail_on_write=1)
        self.assertEqual(state, "RECOVERY_REQUIRED")
        self.assertFalse((self.root / self.new_record).exists())
        self.assertEqual(recovery.recover_fixture(self.root, tx), "RECOVERED")

    def test_foreign_change_blocks_rollback(self):
        state, tx = self.exercise(fail_on_write=2)
        self.assertEqual(state, "RECOVERY_REQUIRED")
        (self.root / self.new_record).write_bytes(b"foreign edit")
        with self.assertRaisesRegex(recovery.FixtureRecoveryError, "recovery_external_conflict"):
            recovery.recover_fixture(self.root, tx)
        self.assertEqual((self.root / self.new_record).read_bytes(), b"foreign edit")

    def test_missing_marker_prevents_any_transaction(self):
        (self.root / recovery.MARKER).unlink()
        with self.assertRaisesRegex(recovery.FixtureRecoveryError, "synthetic_marker_missing"):
            self.exercise()
        self.assertFalse((self.root / self.new_record).exists())

    def test_missing_snapshot_prevents_recovery(self):
        state, tx = self.exercise(fail_on_write=2)
        journal = recovery.load_journal(self.root, tx)
        entry = next(e for e in journal["indexes"] if e["path"] == "00_LIBRARY/MASTER_INDEX.md")
        (self.root / entry["snapshot_path"]).unlink()
        with self.assertRaisesRegex(recovery.FixtureRecoveryError, "verified_recovery_failed"):
            recovery.recover_fixture(self.root, tx)
        self.assertEqual(recovery.load_journal(self.root, tx)["state"], "RECOVERY_REQUIRED")

    def test_existing_journal_blocks_duplicate_execution(self):
        self.exercise()
        with self.assertRaisesRegex(recovery.FixtureRecoveryError, "existing_journal_requires_reconciliation"):
            self.exercise()

    def test_no_write_action_does_not_persist_journal(self):
        self.decisions = self.case.decision_bytes(self.case.decision())
        state, tx = self.exercise()
        self.assertEqual(state, "NO_WRITE_DECISIONS")
        self.assertIsNone(tx)
        self.assertEqual((self.root / "00_LIBRARY/MASTER_INDEX.md").read_bytes(),
                         self.case.initial_master)

    def test_stale_index_fails_before_snapshot_creation(self):
        (self.root / "00_LIBRARY/MASTER_INDEX.md").write_bytes(b"stale")
        with self.assertRaises(Exception):
            self.exercise()
        self.assertFalse((self.root / self.new_record).exists())


if __name__ == "__main__":
    unittest.main()
