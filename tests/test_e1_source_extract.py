"""Real locally encoded file extraction; no Drive, Gemini or source uploads."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import e1_source_extract as extract
import e1_lote_prepare as e1
from ready_evidence_bridge import build_index


@unittest.skipUnless(os.name == "posix", "bounded extraction requires the Linux Actions process boundary")
class TestRealSourceExtraction(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.counter = 0

    def run_extract(self, source, **kwargs):
        self.counter += 1
        return extract.extract_file(source, self.root / f"package-{self.counter}", **kwargs)

    def source(self, name, raw):
        path = self.root / name
        path.write_bytes(raw)
        return path

    def sample(self):
        return "Database transactions preserve atomicity and isolate concurrent writes. " * 4

    def test_real_text_is_hash_bound_and_enters_e1_without_filename(self):
        source = self.source("PRIVATE_NAME_DO_NOT_SEND.txt", self.sample().encode())
        result = self.run_extract(source)
        self.assertEqual(result.assessment["state"], "ELIGIBLE")
        self.assertEqual(result.source_sha256, hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertIn("Database transactions", result.evidence_raw.decode())
        self.assertNotIn(source.name, result.evidence_raw.decode())
        store = e1.Storage(self.root / "drive-mirror")
        def write(path, raw):
            target = store.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        write(e1.MASTER, b"# MASTER_INDEX\nRECORDS: 0\n")
        write(result.envelope["pointers"]["evidence"], result.evidence_raw)
        write(e1.FILE_ROOT + "/" + result.package_id + ".json", e1.encode(result.envelope))
        write(e1.FILE_ROOT + "/index.json", e1.encode(build_index([result.envelope], [])))
        lote, path, held = e1.prepare(store, "2026-11-01")
        self.assertEqual(held, 0)
        self.assertEqual(len(lote["itens"]), 1)
        self.assertEqual(lote["itens"][0]["package_id"], result.package_id)
        self.assertNotIn(source.name, e1.encode(lote).decode())
        store.create(path, e1.encode(lote))
        self.assertIsNone(e1.prepare(store, "2026-11-02")[0])

    def test_same_content_new_filename_has_same_package_identity(self):
        first = self.run_extract(self.source("a.txt", self.sample().encode()))
        second = self.run_extract(self.source("renamed.txt", self.sample().encode()))
        self.assertEqual(first.package_id, second.package_id)
        self.assertEqual(first.evidence_raw, second.evidence_raw)
        self.assertEqual(first.envelope["envelope_id"], second.envelope["envelope_id"])

    def test_real_docx_zip_xml(self):
        path = self.root / "document.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>' + self.sample() + '</w:t></w:r></w:p></w:body></w:document>')
        result = self.run_extract(path)
        self.assertEqual(result.kind, "document")
        self.assertIn("Database transactions", result.evidence_raw.decode())
        self.assertEqual(result.assessment["state"], "ELIGIBLE")

    def test_real_csv(self):
        result = self.run_extract(self.source("data.csv", b"system,guarantee\nDatabase,Atomic transaction\nGit,Compare and swap\n"))
        self.assertEqual(result.kind, "spreadsheet")
        self.assertEqual(result.assessment["state"], "ELIGIBLE")
        self.assertIn("Atomic transaction", result.evidence_raw.decode())

    @unittest.skipUnless(importlib.util.find_spec("openpyxl"), "openpyxl unavailable")
    def test_real_xlsx(self):
        from openpyxl import Workbook
        path = self.root / "workbook.xlsx"
        workbook = Workbook()
        workbook.active.append(["system", "guarantee"])
        workbook.active.append(["Database", "Atomic transaction"])
        workbook.save(path)
        result = self.run_extract(path)
        self.assertEqual(result.assessment["state"], "ELIGIBLE")
        self.assertIn("Atomic transaction", result.evidence_raw.decode())

    @unittest.skipUnless(shutil.which("pdftotext"), "pdftotext unavailable")
    def test_real_pdf(self):
        # A valid standalone PDF, generated without a third-party test dependency.
        text = b"BT /F1 18 Tf 30 150 Td (Atomic database transactions preserve data.) Tj ET"
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
                   b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                   b"<< /Length " + str(len(text)).encode() + b" >>\nstream\n" + text + b"\nendstream"]
        raw, offsets = b"%PDF-1.4\n", [0]
        for n, obj in enumerate(objects, 1):
            offsets.append(len(raw))
            raw += str(n).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
        xref = len(raw)
        raw += b"xref\n0 6\n0000000000 65535 f \n"
        raw += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
        raw += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        result = self.run_extract(self.source("actual.pdf", raw))
        self.assertEqual(result.assessment["state"], "ELIGIBLE")
        self.assertIn("Atomic database transactions", result.evidence_raw.decode())

    def image_source(self):
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new("RGB", (1200, 300), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 42)
        draw.text((30, 80), "Atomic database transactions preserve data", fill="black", font=font)
        path = self.root / "ocr-input.png"
        image.save(path)
        return path

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("tesseract") and importlib.util.find_spec("PIL") and Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf").exists(), "real OCR tools unavailable")
    def test_real_png_ocr(self):
        result = self.run_extract(self.image_source())
        self.assertEqual(result.assessment["state"], "ELIGIBLE")
        self.assertIn("database transactions", result.evidence_raw.decode().lower())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("tesseract") and importlib.util.find_spec("PIL") and Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf").exists(), "real video OCR tools unavailable")
    def test_real_encoded_video_ocr(self):
        image, video = self.image_source(), self.root / "actual.mp4"
        result = subprocess.run(["ffmpeg", "-loglevel", "error", "-loop", "1", "-i", str(image), "-t", "1", "-r", "2", "-threads", "1", "-pix_fmt", "yuv420p", str(video)], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0)
        decoded = self.run_extract(video)
        self.assertEqual(decoded.assessment["state"], "ELIGIBLE")
        self.assertIn("database transactions", decoded.evidence_raw.decode().lower())

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg unavailable")
    def test_real_audio_without_local_model_is_held_not_fake_transcription(self):
        path = self.root / "tone.wav"
        result = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(path)], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0)
        decoded = self.run_extract(path)
        self.assertEqual(decoded.assessment["state"], "HELD")
        self.assertEqual(json.loads(decoded.evidence_raw)["evidence"]["speech"], [])

    def test_malformed_document_and_unsupported_and_link_are_held(self):
        for name, raw in [("broken.pdf", b"not a PDF"), ("broken.docx", b"not a ZIP"), ("legacy.doc", b"legacy"), ("shortcut.url", b"[InternetShortcut]\nURL=https://example.com/\n")]:
            with self.subTest(name=name):
                result = self.run_extract(self.source(name, raw))
                self.assertEqual(result.assessment["state"], "HELD")
                self.assertNotIn("public_link", result.assessment["allowed_channels"])

    def test_source_symlink_and_existing_destination_fail_closed(self):
        source = self.source("source.txt", b"evidence")
        link = self.root / "alias.txt"
        link.symlink_to(source)
        with self.assertRaisesRegex(extract.ExtractionError, "symlink_path_forbidden"):
            self.run_extract(link)
        with self.assertRaisesRegex(extract.ExtractionError, "destination_exists"):
            extract.extract_file(source, self.root)

    def test_size_and_budget_limits(self):
        source = self.source("source.txt", b"longer than budget")
        with patch.object(extract, "MAX_INPUT_BYTES", 4):
            with self.assertRaisesRegex(extract.ExtractionError, "source_budget_exceeded"):
                self.run_extract(source)
        for budget in [0, -1, float("inf"), float("nan"), True, 901]:
            with self.assertRaisesRegex(extract.ExtractionError, "invalid_extraction_budget"):
                self.run_extract(source, budget_seconds=budget)

    def test_real_hung_child_is_killed_by_total_deadline(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        (scripts / "blocked.py").write_text("import time\ntime.sleep(20)\n")
        output = self.root / "output"
        output.mkdir()
        start = time.monotonic()
        with patch.object(extract, "SRC", scripts):
            with self.assertRaisesRegex(extract.ExtractionError, "extraction_deadline"):
                extract._run("blocked.py", [], output, time.monotonic() + 0.15)
        self.assertLess(time.monotonic() - start, 3)

    def test_timeout_kills_descendants_before_they_can_write_late(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        output = self.root / "output"
        output.mkdir()
        late = output / "late-write"
        child = "import pathlib,time;time.sleep(0.6);pathlib.Path(" + repr(str(late)) + ").write_text('late')"
        (scripts / "blocked.py").write_text("import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c'," + repr(child) + "])\ntime.sleep(20)\n")
        with patch.object(extract, "SRC", scripts):
            with self.assertRaisesRegex(extract.ExtractionError, "extraction_deadline"):
                extract._run("blocked.py", [], output, time.monotonic() + 0.2)
        time.sleep(0.7)
        self.assertFalse(late.exists())

    def test_excess_artifacts_abort_the_decoder(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        output = self.root / "output"
        output.mkdir()
        (scripts / "many.py").write_text("import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\n[(p/str(i)).write_text('x') for i in range(10)]\ntime.sleep(20)\n")
        with patch.object(extract, "SRC", scripts), patch.object(extract, "MAX_ARTIFACT_FILES", 4):
            with self.assertRaisesRegex(extract.ExtractionError, "artifact_count_exceeded"):
                extract._run("many.py", [str(output)], output, time.monotonic() + 5)

    def test_decoder_environment_does_not_inherit_secrets_or_emit_content(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        (scripts / "check.py").write_text("import os,pathlib,sys\nassert 'GEMINI_API_KEY' not in os.environ\nassert os.environ['HF_HUB_OFFLINE']=='1'\nprint('PRIVATE_DECODER_OUTPUT')\npathlib.Path(sys.argv[1]).write_text('safe')\n")
        output = self.root / "output"
        output.mkdir()
        with patch.object(extract, "SRC", scripts), patch.dict(os.environ, {"GEMINI_API_KEY": "synthetic-secret"}):
            self.assertTrue(extract._run("check.py", [str(output / "result")], output, time.monotonic() + 5))
        self.assertEqual((output / "result").read_text(), "safe")


if __name__ == "__main__":
    unittest.main()
