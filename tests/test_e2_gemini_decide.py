"""Synthetic E2 decision tests: never call real Gemini or remote Drive."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import e2_gemini_decide as e2
from e1_lote_prepare import LOTES, encode


class TestE2(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = e2.DecisionStore(self.root)

    def fixture(self, *, matches=None, evidence="React Bits provides React UI components."):
        pid = "0" * 19 + "1"
        rev = "0" * 19 + "2"
        sha = "a" * 64
        iid = hashlib.sha256(("e1|" + pid + "|" + rev + "|" + sha).encode()).hexdigest()[:20]
        row = {"item_id": iid, "package_id": pid, "revision_key": rev,
               "evidence_sha256": sha, "kind": "text",
               "evidence_assessment": {"state": "ELIGIBLE"},
               "evidence_compact": {"evidence": {"samples": [evidence]}},
               "matches": matches if matches is not None else []}
        items = [row]
        lote = {"schema_version": 1, "lote_id": "2026-10-27-01",
                "created_at_utc": "2026-10-27T10:00:00+00:00",
                "created_date_brt": "2026-10-27",
                "status": "PRONTO", "itens": items,
                "items_sha256": hashlib.sha256(encode(items)).hexdigest()}
        path = LOTES + "/2026-10-27/lote.json"
        self.store.create(path, encode(lote))
        return lote, path

    def decision(self, item_id, action="HOLD", reason="MODEL_UNCERTAIN", **changes):
        default = {"item_id": item_id, "acao": action, "motivo": reason,
                   "confianca": 0.5, "record_type": None, "route": None,
                   "target_record_id": None, "record_markdown": None, "claims": []}
        default.update(changes)
        return default

    def test_day_without_lote_is_noop(self):
        self.assertEqual(e2.decide(self.store, responses={"decisoes": []}, apply=True), (0, 0))

    def test_held_decision_bytes_bound_and_written(self):
        lote, path = self.fixture()
        item = lote["itens"][0]
        response = {"decisoes": [self.decision(item["item_id"])]}
        count, written = e2.decide(self.store, responses=response, apply=True)
        self.assertEqual((count, written), (1, 1))
        output = self.store.read(LOTES + "/2026-10-27/decisoes.json")
        decoded = e2.strict_json(output)
        self.assertEqual(decoded["lote_sha256"], hashlib.sha256(self.store.read(path)).hexdigest())
        self.assertEqual(decoded["decisoes"][0]["acao"], "HOLD")
        self.assertFalse((self.root / "00_LIBRARY").exists())
        self.assertEqual(e2.decide(self.store, responses=response, apply=True), (0, 0))

    def test_untrusted_action_is_rejected(self):
        lote, path = self.fixture()
        row = self.decision(lote["itens"][0]["item_id"], action="DELETE")
        with self.assertRaisesRegex(e2.DecisionError, "decision_binding_invalid"):
            e2.build_decision_file(self.store.read(path), "2026-10-27/lote.json", {"decisoes": [row]})

    def test_missing_or_repeated_decision_fails(self):
        lote, path = self.fixture()
        raw = self.store.read(path)
        with self.assertRaisesRegex(e2.DecisionError, "decision_count_invalid"):
            e2.build_decision_file(raw, "2026-10-27/lote.json", {"decisoes": []})

    def test_create_with_bound_quote_is_valid(self):
        lote, path = self.fixture()
        item_id = lote["itens"][0]["item_id"]
        record_id = e2.expected_record_id(item_id)
        markdown = ("RECORD_ID: " + record_id + "\nTYPE: TECHNOLOGY\nSTATUS: TEST\n" +
                    "DOMAIN: 02_WEB_DEVELOPMENT\nCATEGORY: UI_COMPONENTS\nTITLE: React Bits\n" +
                    "## SUMMARY\nReact UI components.\n")
        row = self.decision(item_id, action="CREATE", reason=None, confianca=0.92,
                            record_type="TECHNOLOGY", route="02_WEB_DEVELOPMENT/UI_COMPONENTS",
                            record_markdown=markdown,
                            claims=[{"text": "React UI components exist.",
                                     "evidence_quote": "React UI components"}])
        result = e2.build_decision_file(self.store.read(path), "2026-10-27/lote.json",
                                        {"decisoes": [row]})
        self.assertEqual(result["decisoes"][0]["acao"], "CREATE")

    def test_create_with_hallucinated_quote_fails(self):
        lote, path = self.fixture()
        item_id = lote["itens"][0]["item_id"]
        record_id = e2.expected_record_id(item_id)
        markdown = ("RECORD_ID: " + record_id + "\nTYPE: TECHNOLOGY\nSTATUS: TEST\n" +
                    "DOMAIN: 02_WEB_DEVELOPMENT\nCATEGORY: UI_COMPONENTS\n")
        row = self.decision(item_id, action="CREATE", reason=None, confianca=0.9,
                            record_type="TECHNOLOGY", route="02_WEB_DEVELOPMENT/UI_COMPONENTS",
                            record_markdown=markdown,
                            claims=[{"text": "Fake feature", "evidence_quote": "unrelated invented feature"}])
        with self.assertRaisesRegex(e2.DecisionError, "claim_not_grounded"):
            e2.build_decision_file(self.store.read(path), "2026-10-27/lote.json",
                                   {"decisoes": [row]})

    def test_create_blocked_when_canonical_matches_exist(self):
        target = "b" * 20
        lote, path = self.fixture(matches=[{"record_id": target, "title": "React Bits"}])
        item_id = lote["itens"][0]["item_id"]
        row = self.decision(item_id, action="CREATE", reason=None, confianca=0.9,
                            record_type="TECHNOLOGY", route="02_WEB_DEVELOPMENT/UI_COMPONENTS",
                            record_markdown="RECORD_ID: " + e2.expected_record_id(item_id) +
                            "\nTYPE: TECHNOLOGY\nSTATUS: TEST\nDOMAIN: 02_WEB_DEVELOPMENT\nCATEGORY: UI_COMPONENTS\n",
                            claims=[{"text": "One claim", "evidence_quote": "React UI components"}])
        with self.assertRaisesRegex(e2.DecisionError, "create_duplicate_risk"):
            e2.build_decision_file(self.store.read(path), "2026-10-27/lote.json",
                                   {"decisoes": [row]})

    def test_update_to_unmatched_id_is_blocked(self):
        target = "b" * 20
        lote, path = self.fixture(matches=[{"record_id": target}])
        item_id = lote["itens"][0]["item_id"]
        row = self.decision(item_id, action="UPDATE", reason=None, confianca=0.9,
                            record_type="TECHNOLOGY", route="02_WEB_DEVELOPMENT/UI_COMPONENTS",
                            target_record_id="c" * 20,
                            record_markdown="RECORD_ID: " + ("c" * 20) +
                            "\nTYPE: TECHNOLOGY\nSTATUS: TEST\nDOMAIN: 02_WEB_DEVELOPMENT\nCATEGORY: UI_COMPONENTS\n",
                            claims=[{"text": "One claim", "evidence_quote": "React UI components"}])
        with self.assertRaisesRegex(e2.DecisionError, "update_target_invalid"):
            e2.build_decision_file(self.store.read(path), "2026-10-27/lote.json",
                                   {"decisoes": [row]})

    def test_lote_tampering_is_blocked(self):
        lote, path = self.fixture()
        lote["itens"][0]["kind"] = "image"
        (self.root / path).write_bytes(encode(lote))
        with self.assertRaisesRegex(e2.DecisionError, "lote_contract_invalid"):
            e2.decide(self.store, responses={"decisoes": []}, apply=False)

    def test_confidence_below_threshold_blocks_write(self):
        lote, path = self.fixture()
        item_id = lote["itens"][0]["item_id"]
        row = self.decision(item_id, action="CREATE", reason=None, confianca=0.7,
                            record_type="TECHNOLOGY", route="02_WEB_DEVELOPMENT/UI_COMPONENTS",
                            record_markdown="NO RECORD", claims=[])
        with self.assertRaisesRegex(e2.DecisionError, "record_decision_invalid"):
            e2.build_decision_file(self.store.read(path), "2026-10-27/lote.json",
                                   {"decisoes": [row]})

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaisesRegex(e2.DecisionError, "json_invalid"):
            e2.strict_json(b'{"decisoes":[],"decisoes":[]}')

    def test_unexpected_source_metadata_rejected(self):
        lote, path = self.fixture()
        lote["itens"][0]["evidence_compact"]["private_name"] = "no"
        lote["items_sha256"] = hashlib.sha256(encode(lote["itens"])).hexdigest()
        with self.assertRaisesRegex(e2.DecisionError, "item_evidence_invalid"):
            e2.validate_lote(encode(lote), "2026-10-27/lote.json")


if __name__ == "__main__":
    unittest.main()
