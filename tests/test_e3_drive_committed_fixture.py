"""Isolated E3 Drive fixture tests; no remote calls in the security gate."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from e2_gemini_decide import validate_lote, validate_model_output, strict_json
from e3_transaction_preflight import compile_transaction, simulate
from e3_drive_committed_fixture import synthetic_two_item_batch, PREFIX
from tests.test_e3_transaction_preflight import E3PreflightTests


class DriveCommittedContractTests(unittest.TestCase):
    def setUp(self):
        self.case = E3PreflightTests("test_no_write_decision_does_not_touch_canonical")
        self.case.setUp()
        self.addCleanup(self.case.tmp.cleanup)

    def test_two_item_create_update_have_exact_contract(self):
        lot, decisions = synthetic_two_item_batch(self.case)
        lote = validate_lote(lot, self.case.path)
        choices = validate_model_output(lote, {"decisoes": strict_json(decisions)["decisoes"]})
        self.assertEqual([c["acao"] for c in choices], ["CREATE", "UPDATE"])
        plan, staged, paths, settlement = compile_transaction(
            self.case.root, lot, self.case.path, decisions)
        self.assertEqual([item["lane"] for item in plan["items"]], ["NEW", "UPDATE"])
        self.assertEqual(len(staged), 2)
        self.assertEqual(len(paths), 1)
        self.assertEqual([r["acao"] for r in settlement["results"]], ["CREATE", "UPDATE"])
        self.assertEqual(simulate(self.case.root, lot, self.case.path, decisions)["writes_simulated"], 2)
        self.assertFalse((self.case.root / "00_LIBRARY/02_WEB_DEVELOPMENT/UI_COMPONENTS/technology-pixel-rig-tool.md").exists())

    def test_remote_root_is_private_fixture_only(self):
        self.assertTrue(PREFIX.startswith("99_INBOX/CANDIDATES/"))
        self.assertNotIn("00_LIBRARY", PREFIX)
        self.assertNotIn("DROP_HERE", PREFIX)


if __name__ == "__main__":
    unittest.main()
