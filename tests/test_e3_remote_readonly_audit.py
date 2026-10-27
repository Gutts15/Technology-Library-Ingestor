"""E3 production read-only Drive audit: synthetic fake-storage inputs only."""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import e3_remote_readonly_audit as a
from tests.test_e3_transaction_preflight import E3PreflightTests


class TestE3ReadOnly(unittest.TestCase):
    def setUp(self):
        self.fixture = E3PreflightTests("test_no_write_decision_does_not_touch_canonical")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tmp.cleanup)
        self.root = self.fixture.root
        self.operations = []
        self.canonical = {p.relative_to(self.root).as_posix(): p.read_bytes()
                          for p in self.root.rglob("*") if p.is_file()
                          and p.relative_to(self.root).as_posix().startswith("00_LIBRARY/")}
        self.private = {}
        self.fixture_file = self.fixture.decision_bytes(self.fixture.decision())
        self.private[a.LOTES + "/" + self.fixture.path] = self.fixture.lote_raw
        self.private[a.LOTES + "/2026-10-27/decisoes.json"] = self.fixture_file

    def read(self, remote, path, limit):
        self.operations.append(("read", remote, path))
        raw = self.canonical.get(path, self.private.get(path))
        if raw is None:
            raise a.AuditFailure("input_missing")
        if len(raw) > limit:
            raise a.AuditFailure("oversize_read")
        return raw

    def listing(self, remote, path):
        self.operations.append(("list", remote, path))
        data = self.canonical if path == "00_LIBRARY" else self.private
        return sorted(p[len(path) + 1:] for p in data if p.startswith(path + "/"))

    def call(self, pending=False):
        return a.audit("tl:", inspect_pending=pending, read_file=self.read,
                       list_files=self.listing)

    def test_reads_canonical_without_any_mutation(self):
        result = self.call()
        self.assertEqual(result["status"], "CANONICAL_READONLY_VERIFIED")
        self.assertEqual(result["records"], 1)
        self.assertEqual(result["pending"], "NOT_CHECKED")
        self.assertTrue(all(op[0] in {"read", "list"} for op in self.operations))
        self.assertEqual(self.canonical["00_LIBRARY/MASTER_INDEX.md"],
                         self.fixture.initial_master)

    def test_pending_hold_is_simulated_and_canonic_unchanged(self):
        master = self.canonical["00_LIBRARY/MASTER_INDEX.md"]
        result = self.call(pending=True)
        self.assertEqual(result["pending"], "SIMULATED_NO_WRITES")
        self.assertEqual(self.canonical["00_LIBRARY/MASTER_INDEX.md"], master)

    def test_pending_receipt_skips_settled_lot(self):
        self.private[a.LOTES + "/2026-10-27/recibo.json"] = b"{}"
        result = self.call(pending=True)
        self.assertEqual(result["pending"], "NONE")

    def test_master_drift_rejected(self):
        self.canonical["00_LIBRARY/MASTER_INDEX.md"] += b"\nforeign"
        with self.assertRaisesRegex(a.AuditFailure, "master_stale"):
            self.call()

    def test_missing_index_rejected(self):
        self.canonical.pop("00_LIBRARY/02_WEB_DEVELOPMENT/INDEX.md")
        with self.assertRaisesRegex(a.AuditFailure, "input_missing"):
            self.call()

    def test_pending_hash_mismatch_rejected(self):
        self.private[a.LOTES + "/" + self.fixture.path] += b"\n"
        with self.assertRaisesRegex(a.AuditFailure, "pending_transaction_invalid"):
            self.call(pending=True)

    def test_remote_restriction(self):
        with self.assertRaisesRegex(a.AuditFailure, "remote_invalid"):
            a.audit("https://untrusted.example", read_file=self.read,
                    list_files=self.listing)

    def test_unsafe_paths_rejected(self):
        for path in ("00_LIBRARY/../sensitive", "../private", "00_LIBRARY//a",
                     "00_LIBRARY\\sensitive"):
            with self.assertRaisesRegex(a.AuditFailure, "unsafe_read_path"):
                a.read("tl:", path, 100, call=lambda _: self.fail("must not read"))

    def test_inventory_duplicate_and_dotdot_rejected(self):
        def runner(args, timeout=75):
            return b'[{"Path":"a.md"},{"Path":"a.md"}]'
        with self.assertRaisesRegex(a.AuditFailure, "inventory_duplicate"):
            a.inventory("tl:", "00_LIBRARY", call=runner)
        def traversal(args, timeout=75):
            return b'[{"Path":"../sensitive"}]'
        with self.assertRaisesRegex(a.AuditFailure, "inventory_invalid"):
            a.inventory("tl:", "00_LIBRARY", call=traversal)

    def test_invalid_pending_decisions_fail_closed(self):
        self.private[a.LOTES + "/2026-10-27/decisoes.json"] = b'{}'
        with self.assertRaisesRegex(a.AuditFailure, "pending_transaction_invalid"):
            self.call(pending=True)


if __name__ == "__main__":
    unittest.main()
