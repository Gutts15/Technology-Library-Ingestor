"""Offline reconciliation of immutable E1 lots and E3 terminal sidecars."""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tests.test_e1_lote_prepare import TestE1Prepare
from e1_lote_prepare import LOTES, encode, PreparationError
from e2_gemini_decide import MODEL
from e3_lote_state import LotStateError, related_paths, verify_settlement
from e3_terminal_receipt import build_terminal_receipt


class LotSidecarStateTests(unittest.TestCase):
    def setUp(self):
        self.fixture_case = TestE1Prepare("test_empty_index_yields_no_lote")
        self.fixture_case.setUp()
        self.addCleanup(self.fixture_case.temp.cleanup)
        self.fixture_case.fixture()
        self.store = self.fixture_case.store
        self.root = self.fixture_case.root
        self.lot, self.path, _ = self.fixture_case.prepare(self.store, "2026-10-27") if hasattr(self.fixture_case, "prepare") else (None, None, None)
        if self.lot is None:
            from e1_lote_prepare import prepare
            self.lot, self.path, _ = prepare(self.store, "2026-10-27")
        self.raw = encode(self.lot)
        self.store.create(self.path, self.raw)
        self.relative = self.path[len(LOTES) + 1:]
        self.decisions_rel, self.receipt_rel = related_paths(self.relative)

    def response(self):
        row = self.lot["itens"][0]
        return {"schema_version": 1, "lote_id": self.lot["lote_id"],
                "lote_sha256": hashlib.sha256(self.raw).hexdigest(), "model": MODEL,
                "canonical_write_performed": False,
                "decisoes": [{"item_id": row["item_id"], "acao": "HOLD",
                             "motivo": "MODEL_UNCERTAIN", "confianca": 0.6,
                             "record_type": None, "route": None,
                             "target_record_id": None, "record_markdown": None,
                             "claims": []}]}

    def create_sidecar(self):
        raw_decisions = encode(self.response())
        sidecar = build_terminal_receipt(
            self.raw, self.relative, raw_decisions,
            commit_journal=None, verified_receipt_raw=None)
        self.store.create(LOTES + "/" + self.decisions_rel, raw_decisions)
        self.store.create(LOTES + "/" + self.receipt_rel, encode(sidecar))
        return raw_decisions, sidecar

    def test_pending_lot_blocks_new_work_without_receipt(self):
        from e1_lote_prepare import past_state
        _, pending, _ = past_state(self.store)
        self.assertTrue(pending)

    def test_valid_sidecar_unlocks_next_lot_without_reprocessing(self):
        from e1_lote_prepare import past_state, prepare
        self.create_sidecar()
        seen, pending, _ = past_state(self.store)
        self.assertFalse(pending)
        self.assertEqual(len(seen), 1)
        self.fixture_case.fixture(count=2)
        next_lot, next_path, _ = prepare(self.store, "2026-10-28")
        self.assertEqual(len(next_lot["itens"]), 1)
        self.assertNotEqual(next_lot["itens"][0]["item_id"], self.lot["itens"][0]["item_id"])
        self.assertEqual(next_path, LOTES + "/2026-10-28/lote.json")

    def test_receipt_without_decisions_fails_closed(self):
        from e1_lote_prepare import past_state
        self.store.create(LOTES + "/" + self.receipt_rel, b"{}")
        with self.assertRaisesRegex(PreparationError, "settlement_decisions_missing"):
            past_state(self.store)

    def test_edited_lot_digest_rejected(self):
        raw_decisions, receipt = self.create_sidecar()
        tampered = self.raw + b" "
        with self.assertRaisesRegex(LotStateError, "identity_binding_invalid"):
            verify_settlement(tampered, self.relative, raw_decisions, encode(receipt))

    def test_missing_result_rejected(self):
        decisions, receipt = self.create_sidecar()
        receipt["resultados"] = []
        with self.assertRaisesRegex(LotStateError, "rows_count_mismatch"):
            verify_settlement(self.raw, self.relative, decisions, encode(receipt))

    def test_action_disagreement_rejected(self):
        decisions, receipt = self.create_sidecar()
        receipt["resultados"][0]["acao"] = "CREATE"
        with self.assertRaisesRegex(LotStateError, "result_item_unbound"):
            verify_settlement(self.raw, self.relative, decisions, encode(receipt))

    def test_duplicate_json_keys_rejected(self):
        decisions, receipt = self.create_sidecar()
        with self.assertRaisesRegex(LotStateError, "sidecar_json_invalid"):
            verify_settlement(self.raw, self.relative, decisions, b'{"lote_id":"one","lote_id":"two"}')

    def test_unknown_sidecar_does_not_unblock_lot(self):
        from e1_lote_prepare import past_state
        self.store.create(LOTES + "/2026-10-27/random.json", b"{}")
        _, pending, _ = past_state(self.store)
        self.assertTrue(pending)

    def test_sequence_paths(self):
        a, b = related_paths("2026-10-27/lote-02.json")
        self.assertEqual(a, "2026-10-27/decisoes-02.json")
        self.assertEqual(b, "2026-10-27/recibo-02.json")

    def test_write_sidecar_has_no_concurrent_reexecution(self):
        from e1_lote_prepare import past_state
        _, receipt = self.create_sidecar()
        receipt["lote_sha256"] = "0" * 64
        (self.root / LOTES / self.receipt_rel).write_bytes(encode(receipt))
        with self.assertRaisesRegex(PreparationError, "settlement_invalid"):
            past_state(self.store)


if __name__ == "__main__":
    unittest.main()
