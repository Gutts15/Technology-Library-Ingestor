"""Unit tests for E3 immutable private writes: no real rclone invoked."""
from __future__ import annotations
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from e3_private_write import (
    TransportError, allowed_path, reconcile, write_new,
)

TARGET = "99_INBOX/CURATION/LOTES/2026-10-27/recibo.json"
PAYLOAD = b'{"estado":"APLICADO"}\n'
SHA = hashlib.sha256(PAYLOAD).hexdigest()


class TestPrivateWrite(unittest.TestCase):
    def test_success_and_exact_readback(self):
        commands = []
        def run(args, timeout):
            commands.append(args)
            self.assertLessEqual(timeout, 75)
            if args[0] == "copyto":
                self.assertIn("--immutable", args)
                self.assertEqual(Path(args[1]).read_bytes(), PAYLOAD)
                return 0, b""
            return 0, PAYLOAD
        result = write_new("tl:", TARGET, PAYLOAD, run=run)
        self.assertEqual(result.state, "CONFIRMED")
        self.assertEqual(result.sha256, SHA)
        self.assertEqual(len(commands), 2)

    def test_timeout_never_retries(self):
        calls = []
        def run(args, timeout):
            calls.append(args)
            return None
        result = write_new("tl:", TARGET, PAYLOAD, run=run)
        self.assertEqual(result.state, "UNCERTAIN")
        self.assertEqual(len(calls), 1)

    def test_nonzero_status_never_retries(self):
        calls = []
        def run(args, timeout):
            calls.append(args)
            return 1, b""
        self.assertEqual(write_new("tl:", TARGET, PAYLOAD, run=run).state, "UNCERTAIN")
        self.assertEqual(len(calls), 1)

    def test_readback_unreachable_uncertain(self):
        calls = []
        def run(args, timeout):
            calls.append(args)
            return (0, b"") if len(calls) == 1 else None
        self.assertEqual(write_new("tl:", TARGET, PAYLOAD, run=run).state, "UNCERTAIN")
        self.assertEqual(len(calls), 2)

    def test_readback_mismatch_is_conflict(self):
        def run(args, timeout):
            return (0, b"") if args[0] == "copyto" else (0, b"foreign")
        self.assertEqual(write_new("tl:", TARGET, PAYLOAD, run=run).state, "CONFLICT")

    def test_reconcile_uses_read_only_call(self):
        calls = []
        def run(args, timeout):
            calls.append(args)
            return 0, PAYLOAD
        self.assertEqual(reconcile("tl:", TARGET, SHA, run=run).state, "CONFIRMED")
        self.assertEqual(calls, [["cat", "tl:" + TARGET]])

    def test_reconcile_mismatch_fails_closed(self):
        def run(args, timeout):
            return 0, b"DIFFERENT"
        self.assertEqual(reconcile("tl:", TARGET, SHA, run=run).state, "CONFLICT")

    def test_reconcile_inaccessible_remains_uncertain(self):
        def run(args, timeout):
            return 1, b""
        self.assertEqual(reconcile("tl:", TARGET, SHA, run=run).state, "UNCERTAIN")

    def test_canonical_target_never_allowed(self):
        for path in (
            "00_LIBRARY/MASTER_INDEX.md",
            "00_LIBRARY/02_WEB_DEVELOPMENT/INDEX.md",
            "99_INBOX/CURATION/LOTES/2026-10-27/lote.json",
            "99_INBOX/CURATION/LOTES/2026-10-27/decisoes.json",
            "99_INBOX/CURATION/LOTES/2026-10-27/recibo.txt",
            "99_INBOX/CURATION/LOTES/2026-10-27/../recibo.json",
        ):
            self.assertFalse(allowed_path(path))
            with self.assertRaises(TransportError):
                write_new("tl:", path, PAYLOAD, run=lambda *_: self.fail("should not run"))

    def test_snapshot_and_journal_private_paths_allowed(self):
        prefix = ("99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/" + "a" * 20 +
                  "/candidate-publisher-" + "0" * 12 + "/")
        self.assertTrue(allowed_path(prefix + "journal.json"))
        self.assertTrue(allowed_path(prefix + "snapshots/" + "b" * 20 + ".bin"))
        self.assertFalse(allowed_path(prefix + "../../journal.json"))

    def test_missing_payload_blocked(self):
        with self.assertRaises(TransportError):
            write_new("tl:", TARGET, b"", run=lambda *_: self.fail("should not run"))

    def test_invalid_remote_blocked(self):
        with self.assertRaises(TransportError):
            write_new("https://example.com", TARGET, PAYLOAD,
                      run=lambda *_: self.fail("should not run"))


if __name__ == "__main__":
    unittest.main()
