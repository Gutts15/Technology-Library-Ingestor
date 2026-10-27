"""Committed publication reconciliation against the real synthetic E3 engine.

No remote GitHub token, no real Drive writes or production publication.
"""
from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from candidate_committed_reconciliation import (
    proof_errors, reconcile_committed_release,
)
from candidate_production_coordinator import read_state
from e3_commit_fixture import exercise_commit
from e3_recovery_fixture import MARKER, MARKER_BYTES, load_journal
from tests.test_e3_commit_fixture import E3CommittedFixtureTests


class CommittedReconciliationTests(unittest.TestCase):
    def setUp(self):
        helper = E3CommittedFixtureTests("test_create_commits_and_settles_without_duplicate_processing")
        helper.setUp()
        self.addCleanup(helper.case.tmp.cleanup)
        self.helper = helper
        self.root = helper.root
        outcome, tx = exercise_commit(
            helper.root, helper.git, helper.raw_lot, helper.relative, helper.decisions)
        self.assertEqual(outcome, "COMMITTED_RELEASED")
        self.journal = load_journal(self.root, tx)
        self.receipt = (self.root / self.journal["run_root"] / "receipt.json").read_bytes()
        self.free = read_state(str(helper.git))[1]
        self.changes = {}

    def canonical(self, path):
        if path in self.changes:
            return self.changes[path]
        file = self.root / path
        return file.read_bytes() if file.is_file() else None

    def private(self, path):
        file = self.root / path
        return file.read_bytes() if file.is_file() else None

    def active_owner(self):
        return {
            "state": "ACTIVE", "canonical_write_performed": True,
            "transaction_id": self.journal["transaction_id"],
            "batch_id": self.journal["batch_id"],
            "owner": self.journal["owner"],
            "journal_path": self.journal["journal_path"],
        }

    def verify(self, journal=None, receipt=None):
        return proof_errors(
            journal or self.journal,
            self.receipt if receipt is None else receipt,
            read_canonical=self.canonical, read_private=self.private)

    def test_actual_created_transaction_proofs_all_bytes(self):
        self.assertEqual(self.verify(), [])
        result, errors = reconcile_committed_release(
            self.journal, self.receipt, read_canonical=self.canonical,
            read_private=self.private,
            read_owner=lambda: (None, self.free, []),
            release_once=lambda **_: self.fail("no repeat release"),
        )
        self.assertEqual((result, errors), ("ALREADY_RELEASED", []))

    def test_corrupted_canonical_prevents_release(self):
        path = self.journal["items"][0]["target_path"]
        self.changes[path] = b"foreign"
        state, errors = reconcile_committed_release(
            self.journal, self.receipt, read_canonical=self.canonical,
            read_private=self.private,
            read_owner=lambda: self.fail("proof must fail before Git"),
            release_once=lambda **_: self.fail("must not push"),
        )
        self.assertEqual((state, errors), ("BLOCKED", ["commit_canonical_mismatch"]))

    def test_corrupted_snapshot_prevents_release(self):
        snap = next(item["snapshot_path"] for item in self.journal["indexes"]
                    if item.get("snapshot_path"))
        original = self.private
        def corrupt(path):
            return b"changed" if path == snap else original(path)
        result = proof_errors(self.journal, self.receipt,
                              read_canonical=self.canonical, read_private=corrupt)
        self.assertEqual(result, ["commit_snapshot_changed"])

    def test_committed_receipt_digest_prevents_release(self):
        self.assertEqual(self.verify(receipt=self.receipt+b"x"),
                         ["committed_receipt_hash_mismatch"])

    def test_foreign_active_owner_must_not_be_released(self):
        foreign = dict(self.active_owner(), owner="candidate-publisher-" + "f"*12)
        status, errors = reconcile_committed_release(
            self.journal, self.receipt, read_canonical=self.canonical,
            read_private=self.private, read_owner=lambda: (None, foreign, []),
            release_once=lambda **_: self.fail("foreign owner"),
        )
        self.assertEqual((status, errors), ("BLOCKED", ["coordinator_active_identity_mismatch"]))

    def test_lost_ack_of_successful_git_push_confirms_by_read_only_check(self):
        owner = self.active_owner()
        counter = []
        def get_owner():
            return (None, owner, [])
        def release_once(**kwargs):
            counter.append(kwargs)
            owner.clear()
            owner.update(self.free)
            return ["git_push_ack_lost"]
        result, errors = reconcile_committed_release(
            self.journal, self.receipt, read_canonical=self.canonical,
            read_private=self.private,
            read_owner=get_owner, release_once=release_once)
        self.assertEqual((result, errors), ("RELEASED", []))
        self.assertEqual(len(counter), 1)

    def test_lost_ack_without_confirmed_release_does_not_repeat_push(self):
        active = self.active_owner()
        attempts = []
        result, errors = reconcile_committed_release(
            self.journal, self.receipt, read_canonical=self.canonical,
            read_private=self.private, read_owner=lambda: (None, active, []),
            release_once=lambda **kw: attempts.append(kw) or ["git_ack_lost"])
        self.assertEqual((result, errors), ("UNCERTAIN", ["commit_release_unconfirmed"]))
        self.assertEqual(len(attempts), 1)

    def test_foreign_free_state_is_not_success(self):
        foreign = dict(self.free, released_transaction_id="f"*20)
        result, errors = reconcile_committed_release(
            self.journal, self.receipt, read_canonical=self.canonical,
            read_private=self.private, read_owner=lambda: (None, foreign, []),
            release_once=lambda **_: self.fail("no push"),
        )
        self.assertEqual((result, errors), ("BLOCKED", ["coordinator_released_foreign_transaction"]))

    def test_no_after_hash_or_receipt_item_mismatch_prevents_release(self):
        modified = copy.deepcopy(self.journal)
        modified["items"][0].pop("after_sha256")
        self.assertEqual(self.verify(journal=modified), ["commit_after_hash_missing"])
        modified = copy.deepcopy(self.journal)
        modified["items"][0]["record_id"] = "f"*20
        self.assertEqual(self.verify(journal=modified), ["commit_receipt_item_unbound"])


if __name__ == "__main__":
    unittest.main()
