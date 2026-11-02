"""Marco 2: private Drive lease backend of the existing production coordinator.

Uses an in-memory stand-in for rclone so CI needs no credentials. The real
transport is exercised separately against the private synthetic Drive prefix.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import candidate_production_coordinator as c  # noqa: E402

URL = "drive-lease:tl:99_INBOX/CURATION/COORDINATOR_TEST"
OWNER = dict(transaction_id="tx-1", batch_id="lote-1", owner="run-1", journal_path="j/tx-1.json")


class FakeDrive:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.unreachable = False
        self.lose_ack = False  # write lands remotely but caller sees failure
        self.drop_write = False  # write never lands

    def read(self, path):
        if self.unreachable:
            return None
        return self.files.get(path, False)

    def write(self, path, data):
        if self.drop_write:
            return False
        self.files[path] = bytes(data)
        return not self.lose_ack


class DriveLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.drive = FakeDrive()
        self.saved = c._drive_ops
        c._drive_ops = self.drive

    def tearDown(self) -> None:
        c._drive_ops = self.saved

    def test_initialize_and_exclusive_acquire(self) -> None:
        sha, state, errors = c.acquire(URL, **OWNER)
        self.assertEqual(errors, [])
        self.assertEqual(state["state"], "ACTIVE")
        self.assertEqual(state["generation"], 1)
        other = dict(OWNER, transaction_id="tx-2", owner="run-2")
        self.assertEqual(c.acquire(URL, **other)[2], ["coordinator_busy"])

    def test_same_owner_resumes_in_new_process(self) -> None:
        sha1, _, _ = c.acquire(URL, **OWNER)
        sha2, state, errors = c.acquire(URL, **OWNER)
        self.assertEqual((sha1, errors), (sha2, []))
        self.assertEqual(state["owner"], "run-1")

    def test_write_boundary_and_committed_release(self) -> None:
        c.acquire(URL, **OWNER)
        self.assertEqual(c.mark_canonical_write_started(URL, **OWNER), [])
        _, state, _ = c.read_state(URL)
        self.assertTrue(state["canonical_write_performed"])
        self.assertEqual(c.release_prewrite_abort(URL, **OWNER), ["coordinator_release_identity_mismatch"])
        self.assertEqual(c.release_committed(URL, **OWNER, receipt_sha256="a" * 64), [])
        _, state, _ = c.read_state(URL)
        self.assertEqual(state["state"], "FREE")
        self.assertEqual(state["released_transaction_id"], "tx-1")

    def test_prewrite_abort_release(self) -> None:
        c.acquire(URL, **OWNER)
        self.assertEqual(c.release_prewrite_abort(URL, **OWNER), [])
        self.assertEqual(c.read_state(URL)[1]["state"], "FREE")

    def test_lost_acknowledgement_is_not_retried_and_owner_survives(self) -> None:
        c.ensure_initialized(URL)
        self.drive.lose_ack = True
        sha, state, errors = c.acquire(URL, **OWNER)
        self.assertEqual(errors, ["coordinator_acquire_conflict"])
        self.drive.lose_ack = False
        # The write landed: a fresh process sees the exact owner, others stay blocked.
        _, state, _ = c.read_state(URL)
        self.assertEqual((state["state"], state["owner"]), ("ACTIVE", "run-1"))
        self.assertEqual(c.acquire(URL, **dict(OWNER, owner="run-2"))[2], ["coordinator_busy"])
        self.assertEqual(c.acquire(URL, **OWNER)[2], [])

    def test_dropped_write_leaves_state_free(self) -> None:
        c.ensure_initialized(URL)
        self.drive.drop_write = True
        self.assertEqual(c.acquire(URL, **OWNER)[2], ["coordinator_acquire_conflict"])
        self.drive.drop_write = False
        self.assertEqual(c.read_state(URL)[1]["state"], "FREE")

    def test_stale_base_is_rejected_before_write(self) -> None:
        sha, _, _ = c.ensure_initialized(URL)
        _, _, errors = c._drive_prepare(URL, "0" * 64, {"state": "FREE"})
        self.assertEqual(errors, ["coordinator_state_base_changed"])

    def test_late_write_from_old_runner_is_fenced(self) -> None:
        sha_free, _, _ = c.ensure_initialized(URL)
        work, _, _ = c._drive_prepare(URL, sha_free, {"state": "ACTIVE"})
        c.acquire(URL, **OWNER)  # another run wins meanwhile
        self.assertFalse(c._push_expected(work, sha_free))
        self.assertEqual(c.read_state(URL)[1]["owner"], "run-1")

    def test_unreachable_drive_fails_closed(self) -> None:
        self.drive.unreachable = True
        self.assertEqual(c.acquire(URL, **OWNER)[2], ["coordinator_remote_unreachable"])

    def test_git_backend_unchanged_for_git_urls(self) -> None:
        self.assertFalse(c._is_drive("/tmp/bare.git"))
        self.assertTrue(c._is_drive(URL))


if __name__ == "__main__":
    unittest.main()
