"""Remote timeout safety: missing write ACK must never run blind rollback."""
from __future__ import annotations
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from candidate_remote_recovery_probe import RemoteUncertainExit, run_rclone, remote_copy_bytes


class TimeoutSafetyTests(unittest.TestCase):
    def test_rclone_hard_timeout(self):
        with patch("candidate_remote_recovery_probe.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(["rclone", "copyto"], 105)) as m:
            with self.assertRaises(RemoteUncertainExit) as caught:
                run_rclone(["copyto", "a", "b"])
        self.assertEqual(caught.exception.code, 75)
        self.assertEqual(m.call_args.kwargs["timeout"], 105)

    def test_disconnected_storage_exits_uncertain(self):
        with patch("candidate_remote_recovery_probe.subprocess.run",
                   side_effect=OSError("secret token in private error")):
            with self.assertRaises(RemoteUncertainExit) as caught:
                run_rclone(["cat", "tl:private"])
        self.assertEqual(str(caught.exception), "75")

    def test_remote_write_timeout_never_becomes_false_and_rollback(self):
        # The broad rollback-on-False logic of the older controlled publisher
        # MUST NOT handle this uncertain failure as a known negative result.
        with patch("candidate_remote_recovery_probe.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(["rclone"], 105)):
            with self.assertRaises(RemoteUncertainExit):
                remote_copy_bytes("tl:", "99_INBOX/CANDIDATES/E3_PREPARED/abc.md", b"private")

    def test_normal_confirmed_nonzero_result_unchanged(self):
        with patch("candidate_remote_recovery_probe.subprocess.run",
                   return_value=subprocess.CompletedProcess(["rclone"], 1, "", "")):
            result = run_rclone(["cat", "tl:missing"])
        self.assertEqual(result.returncode, 1)

    def test_normal_success_unchanged(self):
        with patch("candidate_remote_recovery_probe.subprocess.run",
                   return_value=subprocess.CompletedProcess(["rclone"], 0, b"ok", b"")):
            result = run_rclone(["cat", "tl:fixture"], binary=True)
        self.assertEqual(result.stdout, b"ok")


if __name__ == "__main__":
    unittest.main()
