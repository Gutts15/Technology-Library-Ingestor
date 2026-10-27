"""E3 remote rollback must prove exact Git ownership before any Drive write."""
from __future__ import annotations
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from candidate_controlled_publish import persist_recovered_and_release


class RemoteRollbackOwnerTests(unittest.TestCase):
    def setUp(self):
        self.journal = {
            "state": "REBUILDING_INDEXES",
            "transaction_id": "a" * 20,
            "batch_id": "b" * 20,
            "owner": "candidate-publisher-" + "c" * 12,
            "journal_path": "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/a/journal.json",
        }
        self.owner = dict(self.journal, state="ACTIVE", canonical_write_performed=True)

    def exercise(self, coordinator_state, errors=None):
        with patch("candidate_controlled_publish.read_coordinator_state",
                   return_value=("d" * 40, coordinator_state, errors or [])), \
             patch("candidate_controlled_publish.persist_journal") as persist, \
             patch("candidate_controlled_publish.execute_recovery") as recover:
            result = persist_recovered_and_release(
                remote="tl:", git_remote_url="unused",
                journal=dict(self.journal), canonical_writer=lambda *_: self.fail("write"))
            return result, persist.call_count, recover.call_count

    def test_foreign_owner_cannot_modify_journal_or_canonical(self):
        other = dict(self.owner, transaction_id="e" * 20)
        result, writes, recovers = self.exercise(other)
        self.assertEqual(result, (False, ["recovery_owner_mismatch"]))
        self.assertEqual((writes, recovers), (0, 0))

    def test_free_coordinator_cannot_trigger_stale_rollback(self):
        result, writes, recovers = self.exercise(dict(self.owner, state="FREE"))
        self.assertEqual(result, (False, ["recovery_owner_mismatch"]))
        self.assertEqual((writes, recovers), (0, 0))

    def test_unarmed_coordinator_refuses_rollback(self):
        result, writes, recovers = self.exercise(dict(self.owner, canonical_write_performed=False))
        self.assertEqual(result, (False, ["recovery_owner_mismatch"]))
        self.assertEqual((writes, recovers), (0, 0))

    def test_unreachable_coordinator_refuses_rollback(self):
        with patch("candidate_controlled_publish.read_coordinator_state",
                   return_value=(None, None, ["network_failed"])), \
             patch("candidate_controlled_publish.persist_journal") as persist:
            result = persist_recovered_and_release(
                remote="tl:", git_remote_url="unused",
                journal=dict(self.journal), canonical_writer=lambda *_: None)
        self.assertEqual(result, (False, ["recovery_owner_unavailable"]))
        persist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
