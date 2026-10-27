"""E1 -> E2 -> E3 machine disposition, entirely offline with synthetic files.

This validates a complete non-canonical HOLD cycle and uninterrupted next-lot
progress. Real CREATE/UPDATE publication remains gated off.
"""
from __future__ import annotations
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tests.test_e1_lote_prepare import TestE1Prepare
from e1_lote_prepare import LOTES, encode, prepare
from e2_gemini_decide import MODEL, DecisionStore, decide
from e3_lote_state import related_paths, verify_settlement
from e3_terminal_receipt import build_terminal_receipt


class SyntheticE123NonwriteIntegration(unittest.TestCase):
    def setUp(self):
        base = TestE1Prepare("test_empty_index_yields_no_lote")
        base.setUp()
        self.addCleanup(base.temp.cleanup)
        self.fixtures = base
        self.store = base.store
        base.fixture(count=1)
        self.first_lot, self.first_path, _ = prepare(self.store, "2026-10-27")
        self.assertEqual(len(self.first_lot["itens"]), 1)
        self.store.create(self.first_path, encode(self.first_lot))
        self.first_relative = self.first_path[len(LOTES)+1:]
        self.decision_store = DecisionStore(base.root)
        self.item_id = self.first_lot["itens"][0]["item_id"]

    def answer(self, action="HOLD", reason="MODEL_UNCERTAIN"):
        return {"decisoes": [{
            "item_id": self.item_id, "acao": action, "motivo": reason,
            "confianca": 0.65, "record_type": None, "route": None,
            "target_record_id": None, "record_markdown": None, "claims": []}]}

    def decisions(self):
        sub, _ = related_paths(self.first_relative)
        return self.store.read(LOTES + "/" + sub)

    def test_automated_no_write_and_next_lot(self):
        count, written = decide(self.decision_store, responses=self.answer(), apply=True)
        self.assertEqual((count, written), (1, 1))
        decision_raw = self.decisions()
        _, receipt_relative = related_paths(self.first_relative)
        receipt = build_terminal_receipt(
            encode(self.first_lot), self.first_relative, decision_raw,
            commit_journal=None, verified_receipt_raw=None)
        self.assertEqual(receipt["resultados"][0]["acao"], "HOLD")
        self.store.create(LOTES + "/" + receipt_relative, encode(receipt))
        self.assertTrue(verify_settlement(encode(self.first_lot), self.first_relative,
                                          decision_raw, self.store.read(LOTES + "/" + receipt_relative))["settled"])
        self.fixtures.fixture(count=2)
        second, path, _ = prepare(self.store, "2026-10-28")
        self.assertEqual(len(second["itens"]), 1)
        self.assertNotEqual(second["itens"][0]["item_id"], self.item_id)
        self.assertTrue(path.endswith("/2026-10-28/lote.json"))
        self.assertFalse((self.fixtures.root / "00_LIBRARY/02_WEB_DEVELOPMENT").exists())

    def test_invalid_e2_action_does_not_create_a_receipt(self):
        from e2_gemini_decide import DecisionError
        with self.assertRaises(DecisionError):
            decide(self.decision_store, responses=self.answer(action="DELETE"), apply=True)
        _, receipt_relative = related_paths(self.first_relative)
        self.assertFalse((self.fixtures.root / LOTES / receipt_relative).exists())
        self.fixtures.fixture(count=2)
        next_lot, _, _ = prepare(self.store, "2026-10-28")
        self.assertIsNone(next_lot)

    def test_missing_terminal_receipt_blocks_new_lot(self):
        decide(self.decision_store, responses=self.answer(), apply=True)
        self.fixtures.fixture(count=2)
        result, _, _ = prepare(self.store, "2026-10-28")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
