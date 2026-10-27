"""Synthetic E1 lot tests. No cloud, secrets, model or real Drive access."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import e1_lote_prepare as e1
from ready_evidence_bridge import build_envelope, build_index


class TestE1Prepare(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = e1.Storage(self.root)
        self.master = self.root / e1.MASTER
        self.master.parent.mkdir(parents=True)
        self.master.write_text("# MASTER_INDEX\nRECORDS: 0\n", encoding="utf-8")

    def write(self, path, data):
        location = self.root / path
        location.parent.mkdir(parents=True, exist_ok=True)
        location.write_bytes(data)
        return location

    def fixture(self, contents="React Bits offers React UI components.", count=1, kind="text"):
        envelopes = []
        for n in range(count):
            pid = format(n + 1, "020x")
            rev = format(n + 101, "020x")
            payload = {"schema_version": 1, "evidence": {"samples": [contents]}}
            raw = e1.encode(payload)
            item = {
                "package_id": pid, "revision_key": rev, "kind": kind,
                "evidence_path": "99_INBOX/READY_FOR_ANALYSIS/" + pid + "/evidence-summary.json",
                "detail_path": None, "preview_path": None,
            }
            envelope, problems = build_envelope(item, raw)
            self.assertEqual(problems, [])
            self.assertIsNotNone(envelope)
            self.write(item["evidence_path"], raw)
            self.write(e1.FILE_ROOT + "/" + pid + ".json", e1.encode(envelope))
            envelopes.append(envelope)
        self.write(e1.FILE_ROOT + "/index.json", e1.encode(build_index(envelopes, [])))
        return envelopes

    def test_empty_index_yields_no_lote(self):
        self.fixture(count=0)
        lote, path, held = e1.prepare(self.store, "2026-10-27")
        self.assertIsNone(lote)
        self.assertIsNone(path)
        self.assertEqual(held, 0)

    def test_create_and_readback_local(self):
        self.fixture()
        lote, path, held = e1.prepare(self.store, "2026-10-27")
        self.assertEqual(lote["status"], "PRONTO")
        self.assertEqual(len(lote["itens"]), 1)
        self.assertEqual(held, 0)
        self.assertEqual(path, e1.LOTES + "/2026-10-27/lote.json")
        self.assertLessEqual(len(e1.encode(lote)), 200 * 1024)
        self.store.create(path, e1.encode(lote))
        self.assertEqual(self.store.read(path), e1.encode(lote))
        self.assertEqual(e1.prepare(self.store, "2026-10-28")[0], None)

    def test_existing_lote_is_immutable(self):
        self.fixture()
        lote, path, _ = e1.prepare(self.store, "2026-10-27")
        raw = e1.encode(lote)
        self.store.create(path, raw)
        self.store.create(path, raw)
        with self.assertRaisesRegex(e1.PreparationError, "lote_conflict"):
            self.store.create(path, b"different")
        self.assertEqual(self.store.read(path), raw)

    def test_held_evidence_is_not_selected(self):
        self.fixture(contents="")
        lote, _, held = e1.prepare(self.store, "2026-10-27")
        self.assertIsNone(lote)
        self.assertEqual(held, 1)

    def test_bound_evidence_tampering_fails_closed(self):
        self.fixture()
        pid = format(1, "020x")
        self.write("99_INBOX/READY_FOR_ANALYSIS/" + pid + "/evidence-summary.json",
                   e1.encode({"schema_version": 1, "evidence": {"samples": ["TAMPERED"]}}))
        with self.assertRaisesRegex(e1.PreparationError, "evidence_binding_invalid"):
            e1.prepare(self.store, "2026-10-27")

    def test_invalid_index_fails_closed(self):
        self.fixture()
        self.write(e1.FILE_ROOT + "/index.json", e1.encode({"schema_version": 1, "type": "FILE_EVIDENCE_INDEX", "items": [], "active": 999}))
        with self.assertRaisesRegex(e1.PreparationError, "index_invalid"):
            e1.prepare(self.store, "2026-10-27")

    def test_bounded_to_twenty_without_destructive_actions(self):
        self.fixture(count=23)
        lote, _, _ = e1.prepare(self.store, "2026-10-27")
        self.assertEqual(len(lote["itens"]), 20)
        self.assertFalse((self.root / e1.LOTES).exists())
        self.assertEqual(len(list((self.root / e1.FILE_ROOT).glob("*.json"))), 24)

    def test_canonical_match_read_only(self):
        self.fixture()
        line = "- TECHNOLOGY - React Bits [REFERENCE] (" + chr(96) + "00_LIBRARY/02_WEB_DEVELOPMENT/UI_COMPONENTS/technology-react-bits.md" + chr(96) + ")\n"
        self.master.write_text("# MASTER_INDEX\n" + line, encoding="utf-8")
        target = "00_LIBRARY/02_WEB_DEVELOPMENT/UI_COMPONENTS/technology-react-bits.md"
        original = "RECORD_ID: 0123456789abcdef0123\nTITLE: React Bits\n## SUMMARY\nA component library.\n"
        self.write(target, original.encode("utf-8"))
        lote, _, _ = e1.prepare(self.store, "2026-10-27")
        matches = lote["itens"][0]["matches"]
        self.assertEqual(matches[0]["record_id"], "0123456789abcdef0123")
        self.assertEqual(matches[0]["summary"], "A component library.")
        self.assertEqual((self.root / target).read_text(encoding="utf-8"), original)

    def test_duplicate_item_id_in_past_lotes_fails_closed(self):
        self.fixture()
        lote, path, _ = e1.prepare(self.store, "2026-10-27")
        self.store.create(path, e1.encode(lote))
        fake = dict(lote, status="APLICADO")
        self.write(e1.LOTES + "/2026-10-26/lote.json", e1.encode(fake))
        with self.assertRaisesRegex(e1.PreparationError, "previous_lote_invalid"):
            e1.prepare(self.store, "2026-10-28")

    def test_read_only_cli_does_not_write(self):
        self.fixture()
        buffer = StringIO()
        with patch.object(sys, "argv", ["e1", "--root-dir", str(self.root), "--date-brt", "2026-10-27"]):
            with redirect_stdout(buffer):
                self.assertEqual(e1.main(), 0)
        self.assertIn("lotes_written=0", buffer.getvalue())
        self.assertFalse((self.root / e1.LOTES).exists())

    def test_invalid_date_cli_fails_closed(self):
        self.fixture()
        buffer = StringIO()
        with patch.object(sys, "argv", ["e1", "--root-dir", str(self.root), "--date-brt", "2026-99-99"]):
            with redirect_stdout(buffer):
                self.assertEqual(e1.main(), 2)
        self.assertIn("canonical_write=0", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
