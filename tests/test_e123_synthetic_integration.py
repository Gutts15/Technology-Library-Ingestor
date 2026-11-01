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


class SyntheticMediaDispositionIntegration(unittest.TestCase):
    """Qualifies extracted evidence contracts, not decoders or live models."""

    def test_media_evidence_dispositions_settle_and_do_not_reprocess(self):
        samples = (
            ("text", {"samples": ["Synthetic tool supports bounded jobs."]}, {}),
            ("document", {"samples": ["Synthetic tool supports bounded jobs."]}, {}),
            ("image", {"ocr_sample": "Synthetic tool supports bounded jobs."}, {}),
            ("spreadsheet", {"row_samples": [["Synthetic tool", "bounded jobs"]]}, {}),
            ("audio", {"speech": [{"text": "Synthetic tool supports bounded jobs."}]},
             {"transcript": {"signal": "high"}}),
            ("video", {"ocr": [{"text": "Synthetic tool supports bounded jobs."}]}, {}),
            ("link", {"samples": ["Synthetic tool supports bounded jobs."]}, {}),
        )
        for kind, evidence, quality in samples:
            for action, reason in (("HOLD", "WEAK_EVIDENCE"), ("REJECT", "IRRELEVANT"),
                                   ("UNCHANGED", "DUPLICATE")):
                with self.subTest(kind=kind, action=action):
                    base = TestE1Prepare("test_empty_index_yields_no_lote")
                    base.setUp()
                    try:
                        initial_master = base.master.read_bytes()
                        base.fixture(kind=kind, extra={"evidence": evidence, "quality": quality})
                        lot, path, held = prepare(base.store, "2026-11-01")
                        self.assertEqual(held, 0)
                        self.assertEqual(lot["itens"][0]["kind"], kind)
                        raw_lot = encode(lot)
                        base.store.create(path, raw_lot)
                        answer = {"decisoes": [{"item_id": lot["itens"][0]["item_id"],
                            "acao": action, "motivo": reason, "confianca": 0.65,
                            "record_type": None, "route": None, "target_record_id": None,
                            "record_markdown": None, "claims": []}]}
                        store = DecisionStore(base.root)
                        self.assertEqual(decide(store, responses=answer, apply=True), (1, 1))
                        relative = path[len(LOTES)+1:]
                        decisions_path, receipt_path = related_paths(relative)
                        raw_decisions = base.store.read(LOTES + "/" + decisions_path)
                        receipt = build_terminal_receipt(raw_lot, relative, raw_decisions,
                            commit_journal=None, verified_receipt_raw=None)
                        base.store.create(LOTES + "/" + receipt_path, encode(receipt))
                        self.assertTrue(verify_settlement(raw_lot, relative, raw_decisions,
                                                         encode(receipt))["settled"])
                        # Next cycle must not invoke a model or enqueue the same evidence.
                        from unittest.mock import patch
                        with patch("e2_gemini_decide.ask_gemini", side_effect=AssertionError("reanalysis")):
                            self.assertEqual(decide(store, apply=True), (0, 0))
                        self.assertIsNone(prepare(base.store, "2026-11-02")[0])
                        self.assertEqual(base.master.read_bytes(), initial_master)
                    finally:
                        base.doCleanups()


if __name__ == "__main__":
    unittest.main()
