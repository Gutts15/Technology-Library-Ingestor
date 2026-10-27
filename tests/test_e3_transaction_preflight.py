"""E3 synthetic-only preflight and in-memory transaction simulation tests."""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import e3_transaction_preflight as e3
from e1_lote_prepare import LOTES, encode
from e2_gemini_decide import expected_record_id
from library_index_build import planned_indexes, validate_record


class E3PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = "2026-10-27/lote.json"
        self.canonical_path = "00_LIBRARY/02_WEB_DEVELOPMENT/UI_COMPONENTS/technology-react-bits.md"
        self.original = (
            "RECORD_ID: 0123456789abcdef0123\nTYPE: TECHNOLOGY\nSTATUS: TEST\n"
            "DOMAIN: 02_WEB_DEVELOPMENT\nCATEGORY: UI_COMPONENTS\n"
            "TITLE: React Bits\n# React Bits\n\n## SUMMARY\n"
            "React Bits provides animated React components.\n"
        ).encode()
        self.write(self.canonical_path, self.original)
        validated = validate_record(self.canonical_path, self.original)
        for path, raw in planned_indexes([validated]).items():
            self.write(path, raw)
        self.initial_master = (self.root / "00_LIBRARY/MASTER_INDEX.md").read_bytes()
        pid, rev, sha = "a" * 20, "b" * 20, "c" * 64
        iid = hashlib.sha256(("e1|" + pid + "|" + rev + "|" + sha).encode()).hexdigest()[:20]
        self.item = {
            "item_id": iid, "package_id": pid, "revision_key": rev, "evidence_sha256": sha,
            "kind": "text", "evidence_assessment": {"state": "ELIGIBLE"},
            "evidence_compact": {"evidence": {"samples": ["Pixel Rig Tool enables rigging workflows for 2D sprites."]}},
            "matches": [],
        }
        self.lote = {"schema_version": 1, "lote_id": "2026-10-27-01",
                     "created_at_utc": "2026-10-27T11:00:00+00:00",
                     "created_date_brt": "2026-10-27", "status": "PRONTO",
                     "itens": [self.item], "items_sha256": hashlib.sha256(encode([self.item])).hexdigest()}
        self.lote_raw = encode(self.lote)
        self.write(LOTES + "/" + self.path, self.lote_raw)

    def write(self, path, raw):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return target

    def decision(self, action="HOLD", **kwargs):
        row = {"item_id": self.item["item_id"], "acao": action,
               "motivo": "MODEL_UNCERTAIN", "confianca": 0.6,
               "record_type": None, "route": None, "target_record_id": None,
               "record_markdown": None, "claims": []}
        row.update(kwargs)
        return row

    def decision_bytes(self, row):
        return encode({"schema_version": 1, "lote_id": self.lote["lote_id"],
                       "lote_sha256": hashlib.sha256(self.lote_raw).hexdigest(),
                       "model": "gemini-3.5-flash-lite", "decisoes": [row],
                       "canonical_write_performed": False})

    def create_row(self, title="Pixel Rig Tool"):
        markdown = ("RECORD_ID: " + expected_record_id(self.item["item_id"]) + "\n"
                    "TYPE: TECHNOLOGY\nSTATUS: TEST\nDOMAIN: 02_WEB_DEVELOPMENT\n"
                    "CATEGORY: UI_COMPONENTS\nTITLE: " + title + "\n# " + title +
                    "\n\n## SUMMARY\nPixel Rig Tool enables rigging workflows for 2D sprites.\n")
        return self.decision("CREATE", motivo=None, confianca=0.95,
                             record_type="TECHNOLOGY", route="02_WEB_DEVELOPMENT/UI_COMPONENTS",
                             record_markdown=markdown, claims=[{"text": "Supports rigging workflows",
                                                               "evidence_quote": "rigging workflows for 2D sprites"}])

    def test_no_write_decision_does_not_touch_canonical(self):
        result = e3.simulate(self.root, self.lote_raw, self.path, self.decision_bytes(self.decision()))
        self.assertEqual(result["state"], "SIMULATED_NO_WRITES")
        self.assertEqual((self.root / self.canonical_path).read_bytes(), self.original)

    def test_create_runs_existing_transaction_engine_in_memory(self):
        before = set(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())
        result = e3.simulate(self.root, self.lote_raw, self.path, self.decision_bytes(self.create_row()))
        self.assertEqual(result["state"], "SIMULATED_VERIFIED")
        self.assertTrue(result["indexes_verified"])
        self.assertEqual(result["writes_simulated"], 1)
        after = set(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())
        self.assertEqual(before, after)
        self.assertEqual((self.root / "00_LIBRARY/MASTER_INDEX.md").read_bytes(), self.initial_master)

    def test_decision_hash_mismatch_fails(self):
        parsed = self.decision_bytes(self.decision()).decode().replace(
            hashlib.sha256(self.lote_raw).hexdigest(), "0" * 64)
        with self.assertRaisesRegex(e3.PreflightError, "decisions_binding_invalid"):
            e3.simulate(self.root, self.lote_raw, self.path, parsed.encode())

    def test_canonical_collision_blocks_create(self):
        self.item["evidence_compact"]["evidence"]["samples"] = ["React Bits provides animated React components."]
        self.lote["items_sha256"] = hashlib.sha256(encode(self.lote["itens"])).hexdigest()
        self.lote_raw = encode(self.lote)
        row = self.create_row(title="React Bits")
        row["claims"][0]["evidence_quote"] = "animated React components"
        with self.assertRaisesRegex(e3.PreflightError, "create_canonical_collision"):
            e3.simulate(self.root, self.lote_raw, self.path, self.decision_bytes(row))

    def test_stale_canonical_index_blocks_any_simulation(self):
        self.write("00_LIBRARY/MASTER_INDEX.md", self.initial_master + b"\n# injected extra")
        with self.assertRaisesRegex(e3.PreflightError, "canonical_index_stale"):
            e3.simulate(self.root, self.lote_raw, self.path, self.decision_bytes(self.decision()))

    def test_hallucinated_quote_is_blocked(self):
        row = self.create_row()
        row["claims"][0]["evidence_quote"] = "This is not evidence"
        with self.assertRaisesRegex(e3.PreflightError, "decisions_invalid"):
            e3.simulate(self.root, self.lote_raw, self.path, self.decision_bytes(row))

    def test_update_requires_preserved_original_content(self):
        self.item["evidence_compact"]["evidence"]["samples"] = ["React Bits provides animated React components. A standalone export workflow is now supported."]
        self.item["matches"] = [{"record_id": "0123456789abcdef0123", "title": "React Bits"}]
        self.lote["items_sha256"] = hashlib.sha256(encode(self.lote["itens"])).hexdigest()
        self.lote_raw = encode(self.lote)
        updated = self.original.decode() + "\n## EXTRA\nNew claims about React Bits.\n"
        row = self.decision("UPDATE", motivo=None, confianca=0.95,
                            record_type="TECHNOLOGY", route="02_WEB_DEVELOPMENT/UI_COMPONENTS",
                            target_record_id="0123456789abcdef0123",
                            record_markdown=updated,
                            claims=[{"text": "Standalone export workflow supported", "evidence_quote": "standalone export workflow"}])
        result = e3.simulate(self.root, self.lote_raw, self.path, self.decision_bytes(row))
        self.assertEqual(result["writes_simulated"], 1)
        self.assertEqual((self.root / self.canonical_path).read_bytes(), self.original)
        row["record_markdown"] = updated.replace("React Bits provides animated React components. A standalone export workflow is now supported.", "Old facts removed.")
        plan, staged, _, _ = e3.compile_transaction(
            self.root, self.lote_raw, self.path, self.decision_bytes(row))
        merged = staged[plan["items"][0]["content_path"]].decode("utf-8")
        self.assertIn(self.original.decode().strip(), merged)
        self.assertIn("Standalone export workflow supported", merged)
        self.assertNotIn("Old facts removed", merged)
        self.assertEqual((self.root / self.canonical_path).read_bytes(), self.original)

    def test_append_update_preserves_reference_status(self):
        base = self.original.replace(b"STATUS: TEST", b"STATUS: REFERENCE")
        self.write(self.canonical_path, base)
        from library_index_build import planned_indexes, validate_record
        for path, raw in planned_indexes(
                [validate_record(self.canonical_path, base)]).items():
            self.write(path, raw)
        self.item["evidence_compact"]["evidence"]["samples"] = [
            "React Bits provides animated React components. A standalone export workflow is now supported."
        ]
        self.item["matches"] = [{"record_id": "0123456789abcdef0123", "title": "React Bits"}]
        self.lote["items_sha256"] = hashlib.sha256(encode(self.lote["itens"])).hexdigest()
        self.lote_raw = encode(self.lote)
        proposal = base.decode().replace("STATUS: REFERENCE", "STATUS: TEST")
        row = self.decision(
            "UPDATE", motivo=None, confianca=0.95,
            record_type="TECHNOLOGY", route="02_WEB_DEVELOPMENT/UI_COMPONENTS",
            target_record_id="0123456789abcdef0123",
            record_markdown=proposal,
            claims=[{"text": "Standalone export workflow supported", "evidence_quote": "standalone export workflow"}])
        plan, staged, _, _ = e3.compile_transaction(
            self.root, self.lote_raw, self.path, self.decision_bytes(row))
        merged = staged[plan["items"][0]["content_path"]]
        self.assertIn(b"STATUS: REFERENCE", merged)
        self.assertTrue(merged.startswith(base.rstrip(b"\n")))
        self.assertIn(b"Evidence excerpt", merged)
        self.assertEqual((self.root / self.canonical_path).read_bytes(), base)

    def test_append_update_rejects_claim_with_markdown_injection(self):
        with self.assertRaisesRegex(e3.PreflightError, "update_claim_unsafe"):
            e3.append_verified_claims(
                self.original,
                [{"text": "Feature [click](https://unsafe.example)", "evidence_quote": "supported quote"}])

    def test_append_update_rejects_duplicate_claim_in_record(self):
        with self.assertRaisesRegex(e3.PreflightError, "update_claim_already_present"):
            e3.append_verified_claims(
                self.original,
                [{"text": "React Bits provides animated React components.",
                  "evidence_quote": "animated React components"}])

    def test_research_type_not_canonical(self):
        row = self.create_row()
        row["record_type"] = "RESEARCH"
        row["route"] = "RESEARCH/ARTICLES"
        row["record_markdown"] = row["record_markdown"].replace("TYPE: TECHNOLOGY", "TYPE: RESEARCH").replace(
            "DOMAIN: 02_WEB_DEVELOPMENT", "DOMAIN: RESEARCH").replace("CATEGORY: UI_COMPONENTS", "CATEGORY: ARTICLES")
        with self.assertRaisesRegex(e3.PreflightError, "unsupported_canonical_type"):
            e3.simulate(self.root, self.lote_raw, self.path, self.decision_bytes(row))


if __name__ == "__main__":
    unittest.main()
