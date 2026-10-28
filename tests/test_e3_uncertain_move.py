"""Production E3 must never interpret ambiguous remote writes as rollback permission."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import candidate_controlled_publish as publish
from candidate_remote_recovery_probe import RemoteUncertainExit


class E3UncertainMoveTests(unittest.TestCase):
    def test_private_receipt_copy_lost_ack_blocks_without_readback(self):
        with patch.object(publish, "remote_copy_bytes", return_value=False), \
             patch.object(publish, "remote_cat") as read:
            with self.assertRaises(RemoteUncertainExit):
                publish._private_write_exact(
                    "tl:", "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/item/receipt.json",
                    b"sample")
            read.assert_not_called()

    def test_private_journal_readback_mismatch_is_uncertain(self):
        with patch.object(publish, "remote_copy_bytes", return_value=True), \
             patch.object(publish, "remote_cat", return_value=None):
            with self.assertRaises(RemoteUncertainExit):
                publish._private_write_exact(
                    "tl:", "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/item/journal.json",
                    b"new journal")

    def test_moveto_nonzero_never_turns_into_false_rollback_signal(self):
        with patch.object(publish, "_private_write_exact", return_value=True), \
             patch.object(publish, "run_rclone", return_value=subprocess.CompletedProcess(
                 ["rclone", "moveto"], 7, "", "")), \
             patch.object(publish, "remote_cat") as read:
            with self.assertRaises(RemoteUncertainExit):
                publish.canonical_atomic_writer(
                    "tl:", "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/synthetic"
                )("00_LIBRARY/02_WEB_DEVELOPMENT/UI_COMPONENTS/technology-example.md", b"test")
            read.assert_not_called()

    def test_moveto_success_but_missing_readback_is_uncertain(self):
        with patch.object(publish, "_private_write_exact", return_value=True), \
             patch.object(publish, "run_rclone", return_value=subprocess.CompletedProcess(
                 ["rclone", "moveto"], 0, "", "")), \
             patch.object(publish, "remote_cat", return_value=None):
            with self.assertRaises(RemoteUncertainExit):
                publish.canonical_atomic_writer(
                    "tl:", "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/synthetic"
                )("00_LIBRARY/MASTER_INDEX.md", b"# verified")

    def test_confirmed_move_must_match_exact_bytes(self):
        content = b"# verified"
        with patch.object(publish, "_private_write_exact", return_value=True), \
             patch.object(publish, "run_rclone", return_value=subprocess.CompletedProcess(
                 ["rclone", "moveto"], 0, "", "")), \
             patch.object(publish, "remote_cat", return_value=content):
            self.assertTrue(publish.canonical_atomic_writer(
                "tl:", "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/synthetic"
            )("00_LIBRARY/MASTER_INDEX.md", content))

    def test_delete_failure_does_not_claim_rollback(self):
        with patch.object(publish, "run_rclone", return_value=subprocess.CompletedProcess(
                ["rclone", "deletefile"], 1, "", "")), \
             patch.object(publish, "remote_cat") as read:
            with self.assertRaises(RemoteUncertainExit):
                publish.canonical_delete("tl:", "00_LIBRARY/MASTER_INDEX.md")
            read.assert_not_called()

    def test_unsafe_target_never_calls_remote(self):
        with patch.object(publish, "_private_write_exact") as write, \
             patch.object(publish, "run_rclone") as move:
            self.assertFalse(publish.canonical_atomic_writer(
                "tl:", "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/synthetic"
            )("../outside.md", b"x"))
            write.assert_not_called()
            move.assert_not_called()


if __name__ == "__main__":
    unittest.main()
