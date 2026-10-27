"""Pure E3 terminal-receipt planner: no remote or local mutations."""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tests.test_e3_transaction_preflight import E3PreflightTests
from e1_lote_prepare import encode
from e3_terminal_receipt import SettlementError, build_terminal_receipt
from e2_gemini_decide import expected_record_id
from library_index_build import planned_indexes, validate_record


class TerminalReceiptTests(unittest.TestCase):
    def setUp(self):
        self.fixture = E3PreflightTests("test_no_write_decision_does_not_touch_canonical")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tmp.cleanup)
        self.root = self.fixture.root
        self.raw_lote = self.fixture.lote_raw
        self.relative = self.fixture.path

    def build(self, raw_decisions, journal=None, receipt=None):
        return build_terminal_receipt(
            self.raw_lote, self.relative, raw_decisions,
            commit_journal=journal,
            verified_receipt_raw=receipt,
            read_canonical=lambda p: (self.root / p).read_bytes() if (self.root / p).is_file() else None)

    def committed_create(self):
        decision = self.fixture.create_row()
        raw_decisions = self.fixture.decision_bytes(decision)
        markdown = decision["record_markdown"].encode("utf-8")
        target = "00_LIBRARY/02_WEB_DEVELOPMENT/UI_COMPONENTS/technology-pixel-rig-tool.md"
        self.fixture.write(target, markdown)
        prior = self.fixture.initial_master
        original = validate_record(self.fixture.canonical_path, self.fixture.original)
        created = validate_record(target, markdown)
        for path, raw in planned_indexes([original, created]).items():
            self.fixture.write(path, raw)
        after = (self.root / "00_LIBRARY/MASTER_INDEX.md").read_bytes()
        record_id = expected_record_id(self.fixture.item["item_id"])
        raw_receipt = encode({
            "state": "VERIFIED_COMMITTED_STATE",
            "verification_only": True,
            "candidate_settlement_eligible": True,
            "transaction_id": "e" * 20,
            "master_index_after_sha256": hashlib.sha256(after).hexdigest(),
            "items": [{
                "candidate_id": self.fixture.item["item_id"],
                "action": "CREATE", "target_path": target,
                "record_id": record_id, "content_sha256": hashlib.sha256(markdown).hexdigest(),
            }],
        })
        journal = {
            "transaction_id": "e" * 20, "state": "COMMITTED",
            "canonical_write_performed": True, "snapshots_verified": True,
            "live_preconditions_verified_under_ownership": True,
            "coordinator_release_allowed": True,
            "readiness_sha256": hashlib.sha256(self.raw_lote).hexdigest(),
            "authorization_sha256": hashlib.sha256(raw_decisions).hexdigest(),
            "receipt_sha256": hashlib.sha256(raw_receipt).hexdigest(),
            "master_index_before_sha256": hashlib.sha256(prior).hexdigest(),
            "items": [{"action": "CREATE", "record_id": record_id,
                       "target_path": target, "before_sha256": None}],
        }
        return raw_decisions, journal, raw_receipt, target

    def test_no_write_only_returns_terminal_machine_outcome(self):
        raw = self.fixture.decision_bytes(self.fixture.decision())
        result = self.build(raw)
        self.assertEqual(result["estado"], "APLICADO")
        self.assertEqual(result["resultados"][0]["acao"], "HOLD")
        self.assertEqual(result["index_verification"], "UNCHANGED")
        self.assertFalse(result["canonical_write_performed_by_receipt_planner"])

    def test_valid_committed_create_records_hashes(self):
        raw, journal, receipt, target = self.committed_create()
        result = self.build(raw, journal, receipt)
        self.assertEqual(result["index_verification"], "VERIFIED")
        self.assertIsNone(result["resultados"][0]["before_sha256"])
        self.assertEqual(result["resultados"][0]["record_id"], journal["items"][0]["record_id"])
        self.assertEqual(result["resultados"][0]["after_sha256"],
                         hashlib.sha256((self.root / target).read_bytes()).hexdigest())
        self.assertEqual(result["status_storage"], "IMMUTABLE_SIDECAR")

    def test_lote_digest_tampering_rejected(self):
        raw = self.fixture.decision_bytes(self.fixture.decision())
        corrupted = raw.replace(hashlib.sha256(self.raw_lote).hexdigest().encode(), b"0" * 64)
        with self.assertRaisesRegex(SettlementError, "decision_document_unbound"):
            self.build(corrupted)

    def test_committed_receipt_must_match_durable_journal(self):
        raw, journal, receipt, _ = self.committed_create()
        journal["receipt_sha256"] = "a" * 64
        with self.assertRaisesRegex(SettlementError, "transaction_unverified"):
            self.build(raw, journal, receipt)

    def test_canonical_target_changed_after_commit(self):
        raw, journal, receipt, target = self.committed_create()
        self.fixture.write(target, b"unexpected external change")
        with self.assertRaisesRegex(SettlementError, "committed_content_changed"):
            self.build(raw, journal, receipt)

    def test_master_index_changed_after_commit(self):
        raw, journal, receipt, _ = self.committed_create()
        self.fixture.write("00_LIBRARY/MASTER_INDEX.md", b"invalid")
        with self.assertRaisesRegex(SettlementError, "master_commit_changed"):
            self.build(raw, journal, receipt)

    def test_duplicate_receipt_rows_fail_closed(self):
        raw, journal, receipt, _ = self.committed_create()
        import json
        payload = json.loads(receipt)
        payload["items"].append(dict(payload["items"][0]))
        receipt = encode(payload)
        journal["receipt_sha256"] = hashlib.sha256(receipt).hexdigest()
        with self.assertRaisesRegex(SettlementError, "receipt_duplicate_item"):
            self.build(raw, journal, receipt)

    def test_unauthorized_commit_evidence_with_only_hold_rejected(self):
        raw = self.fixture.decision_bytes(self.fixture.decision())
        with self.assertRaisesRegex(SettlementError, "unexpected_commit_for_nonwrite"):
            self.build(raw, {}, b"{}")


if __name__ == "__main__":
    unittest.main()
