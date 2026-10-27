"""E3 plan -> existing canonical engine -> verified receipt compatibility tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tests.test_e3_transaction_preflight import E3PreflightTests
from candidate_canonical_transaction_engine import execute_canonical_transaction
from candidate_transaction_receipt import build_receipt
from e3_transaction_preflight import compile_transaction


class E3ReceiptContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = E3PreflightTests("test_no_write_decision_does_not_touch_canonical")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tmp.cleanup)
        self.root = self.fixture.root

    def test_create_plan_produces_verified_existing_receipt(self):
        row = self.fixture.create_row()
        plan, staged, record_paths, settlement = compile_transaction(
            self.root, self.fixture.lote_raw, self.fixture.path,
            self.fixture.decision_bytes(row))
        self.assertEqual(plan["items"][0]["lane"], "NEW")
        self.assertEqual(plan["items"][0]["package_id"], self.fixture.item["package_id"])
        self.assertEqual(plan["items"][0]["revision_key"], self.fixture.item["revision_key"])
        self.assertEqual(plan["items"][0]["candidate_id"], self.fixture.item["item_id"])
        memory = dict(staged)

        def reader(p):
            return memory.get(p, (self.root / p).read_bytes() if (self.root / p).is_file() else None)

        def writer(p, data):
            memory[p] = data
            return True

        outcome, errors = execute_canonical_transaction(
            plan, canonical_record_paths_before=record_paths, read_bytes=reader,
            write_record_bytes=writer, write_index_bytes=writer)
        self.assertEqual(errors, [])
        self.assertEqual(outcome["state"], "CANONICAL_STATE_VERIFIED")
        old_master = (self.root / "00_LIBRARY/MASTER_INDEX.md").read_bytes()
        receipt, errors = build_receipt(
            plan, old_master, reader("00_LIBRARY/MASTER_INDEX.md"), reader)
        self.assertEqual(errors, [])
        self.assertEqual(receipt["state"], "VERIFIED_COMMITTED_STATE")
        self.assertTrue(receipt["candidate_settlement_eligible"])
        self.assertEqual(receipt["items"][0]["candidate_id"], self.fixture.item["item_id"])
        self.assertFalse((self.root / "00_LIBRARY/02_WEB_DEVELOPMENT/UI_COMPONENTS/technology-pixel-rig-tool.md").exists())

    def test_update_plan_produces_verified_existing_receipt(self):
        self.fixture.item["evidence_compact"]["evidence"]["samples"] = [
            "React Bits provides animated React components. A standalone export workflow is now supported."
        ]
        self.fixture.item["matches"] = [{"record_id": "0123456789abcdef0123", "title": "React Bits"}]
        from e1_lote_prepare import encode
        import hashlib
        self.fixture.lote["items_sha256"] = hashlib.sha256(
            encode(self.fixture.lote["itens"])).hexdigest()
        self.fixture.lote_raw = encode(self.fixture.lote)
        updated = self.fixture.original.decode() + "\n## EXTRA\nAnimated React components are included.\n"
        row = self.fixture.decision(
            "UPDATE", motivo=None, confianca=0.95, record_type="TECHNOLOGY",
            route="02_WEB_DEVELOPMENT/UI_COMPONENTS",
            target_record_id="0123456789abcdef0123",
            record_markdown=updated,
            claims=[{"text": "Standalone export workflow supported",
                     "evidence_quote": "standalone export workflow"}])
        plan, staged, record_paths, _ = compile_transaction(
            self.root, self.fixture.lote_raw, self.fixture.path,
            self.fixture.decision_bytes(row))
        self.assertEqual(plan["items"][0]["lane"], "UPDATE")
        state = dict(staged)

        def reader(p):
            return state.get(p, (self.root / p).read_bytes() if (self.root / p).is_file() else None)

        def write(p, raw):
            state[p] = raw
            return True

        report, errors = execute_canonical_transaction(
            plan, canonical_record_paths_before=record_paths,
            read_bytes=reader, write_record_bytes=write, write_index_bytes=write)
        self.assertEqual(errors, [])
        self.assertEqual(report["state"], "CANONICAL_STATE_VERIFIED")
        receipt, errors = build_receipt(
            plan, self.fixture.initial_master,
            reader("00_LIBRARY/MASTER_INDEX.md"), reader)
        self.assertEqual(errors, [])
        self.assertEqual(receipt["counts"]["update"], 1)
        self.assertEqual(receipt["items"][0]["candidate_id"], self.fixture.item["item_id"])
        self.assertEqual((self.root / self.fixture.canonical_path).read_bytes(), self.fixture.original)

    def test_legacy_e3_lane_is_rejected_by_actual_receipt_validator(self):
        row = self.fixture.create_row()
        plan, staged, paths, _ = compile_transaction(
            self.root, self.fixture.lote_raw, self.fixture.path,
            self.fixture.decision_bytes(row))
        plan["items"][0]["lane"] = "E3"
        for item in plan["items"]:
            staged[item["target_path"]] = staged[item["content_path"]]
        def read(p):
            return staged.get(p, (self.root / p).read_bytes() if (self.root / p).is_file() else None)
        receipt, errors = build_receipt(
            plan, self.fixture.initial_master, self.fixture.initial_master, read)
        self.assertIsNone(receipt)
        self.assertIn("item_0_lane", errors)


if __name__ == "__main__":
    unittest.main()
