"""E3 strict recovery must preflight every snapshot and every live path."""
from __future__ import annotations
import copy
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from candidate_production_recovery_engine import execute_recovery
from candidate_controlled_publish import bind_expected_post_hashes

def sha(data):
    return hashlib.sha256(data).hexdigest()


class StrictRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.record = "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-existing.md"
        self.created = "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-created.md"
        self.master = "00_LIBRARY/MASTER_INDEX.md"
        self.before = {self.record: b"old record\n", self.master: b"# Master old\n"}
        self.after = {self.record: b"new record\n", self.created: b"created\n",
                      self.master: b"# Master new\n"}
        self.storage = dict(self.after)
        self.snapshots = {}
        self.journal = {
            "state": "RECOVERY_REQUIRED", "transaction_id": "a" * 20,
            "batch_id": "b" * 20, "owner": "candidate-publisher-" + "c" * 12,
            "journal_path": "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/a/journal.json",
            "canonical_write_performed": True, "snapshots_verified": True,
            "live_preconditions_verified_under_ownership": True,
            "master_index_before_sha256": sha(self.before[self.master]),
            "items": [], "indexes": [],
        }
        for path, value in self.after.items():
            pre = self.before.get(path)
            snap = "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/a/" + sha(path.encode())[:20] + ".bin" if pre else None
            if snap:
                self.snapshots[snap] = pre
            entry = {"existed_before": pre is not None,
                     "before_sha256": sha(pre) if pre is not None else None,
                     "after_sha256": sha(value), "snapshot_path": snap}
            if path == self.master:
                self.journal["indexes"].append(dict(entry, path=path))
            else:
                self.journal["items"].append(dict(entry, target_path=path))
        self.writes = []

    def restore(self):
        def writer(p, raw):
            self.writes.append(p)
            self.storage[p] = raw
            return True
        def remove(p):
            self.writes.append(p)
            self.storage.pop(p, None)
            return True
        return execute_recovery(
            self.journal, read_canonical=self.storage.get,
            read_snapshot=self.snapshots.get, write_canonical=writer,
            delete_canonical=remove, verify_current_state=True)

    def test_rollback_fully_verified(self):
        result, errors = self.restore()
        self.assertEqual(errors, [])
        self.assertEqual(result["state"], "RECOVERED")
        self.assertEqual(self.storage, self.before)

    def test_external_edit_prevents_every_write_even_if_late_index_path(self):
        self.storage[self.master] = b"third party index\n"
        result, errors = self.restore()
        self.assertIsNone(result)
        self.assertEqual(errors, ["recovery_external_conflict"])
        self.assertEqual(self.writes, [])
        self.assertEqual(self.storage[self.master], b"third party index\n")

    def test_missing_post_hash_prevents_legacy_blind_rollback(self):
        self.journal["items"][0].pop("after_sha256")
        result, errors = self.restore()
        self.assertIsNone(result)
        self.assertEqual(errors, ["recovery_expected_after_missing"])
        self.assertEqual(self.writes, [])

    def test_bad_snapshot_prevents_every_write(self):
        first = self.journal["items"][0]["snapshot_path"]
        self.snapshots[first] = b"corrupt"
        result, errors = self.restore()
        self.assertIsNone(result)
        self.assertEqual(errors, ["recovery_snapshot_unverified"])
        self.assertEqual(self.writes, [])

    def test_partially_written_state_is_acceptable_with_valid_hashes(self):
        self.storage[self.master] = self.before[self.master]
        self.storage.pop(self.created)
        result, errors = self.restore()
        self.assertEqual(errors, [])
        self.assertEqual(self.storage, self.before)

    def test_expected_post_hashes_bound_to_actual_transaction_engine(self):
        from tests.test_e3_transaction_preflight import E3PreflightTests
        from e3_recovery_fixture import prepare_fixture, MARKER, MARKER_BYTES
        case = E3PreflightTests("test_no_write_decision_does_not_touch_canonical")
        case.setUp()
        self.addCleanup(case.tmp.cleanup)
        case.write(MARKER, MARKER_BYTES)
        lot, decision = case.lote_raw, case.decision_bytes(case.create_row())
        plan, staged, paths, journal = prepare_fixture(case.root, lot, case.path, decision)
        for group in ("items", "indexes"):
            for entry in journal[group]:
                entry.pop("after_sha256", None)
        errors = bind_expected_post_hashes(
            journal, plan, paths,
            lambda p: (case.root / p).read_bytes() if (case.root / p).is_file() else None)
        self.assertEqual(errors, [])
        self.assertTrue(all(entry.get("after_sha256") for group in ("items", "indexes")
                            for entry in journal[group]))


if __name__ == "__main__":
    unittest.main()
