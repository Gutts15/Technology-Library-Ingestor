"""Real synthetic file decoding through the actual evidence/candidate boundary.

FFmpeg, Tesseract, pdftotext and spreadsheet/XML parsers are real. Public source
fetch and model response are isolated stubs; this is not model qualification,
speech transcription, private Drive integration or canonical publication.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evidence_quality import extracted_text
from file_evidence_candidate_bridge import FILE_ROOT, Storage, run
from ready_evidence_bridge import build_envelope, build_index, expected_evidence_path

FACT = "Public Tool provides offline workflows."
SOURCE_ID = "synthetic-storage-id-canary"
NAME_CANARY = "TL_SOURCE_NAME_CANARY"
URL = "https://example.invalid/public-tool"


def execute(arguments: list[str]) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=45)
    if result.returncode:
        # Do not echo decoder stderr, paths or decoded input content.
        raise AssertionError("synthetic_decoder_failed")
    return result.stdout + result.stderr


def make_pdf(path: Path) -> None:
    stream = f"BT /F1 18 Tf 72 720 Td ({FACT}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for position, item in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{position} 0 obj\n".encode() + item + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    data.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(data)


class MultimodalEvidenceIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        missing = [name for name in ("ffmpeg", "ffprobe", "pdftotext", "tesseract")
                   if not shutil.which(name)]
        if missing:
            raise RuntimeError("required_real_decoder_missing")
        import openpyxl

        cls.temporary = tempfile.TemporaryDirectory(prefix="tl-synthetic-formats-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.inputs = cls.root / "inputs"
        cls.inputs.mkdir()
        cls.sources: dict[str, Path] = {}

        for extension in ("txt", "pdf", "docx", "pptx", "csv", "xlsx", "png", "mp4", "wav"):
            cls.sources[extension] = cls.inputs / f"{NAME_CANARY}.{extension}"
        cls.sources["txt"].write_text(FACT + "\nAutomação técnica em português.", encoding="utf-8")
        make_pdf(cls.sources["pdf"])
        with ZipFile(cls.sources["docx"], "w") as archive:
            archive.writestr("word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:body><w:p><w:r><w:t>{FACT}</w:t></w:r></w:p></w:body></w:document>')
        with ZipFile(cls.sources["pptx"], "w") as archive:
            archive.writestr("ppt/slides/slide1.xml",
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                f'<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{FACT}</a:t>'
                '</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>')
        cls.sources["csv"].write_text(f"tool,capability\nPublic Tool,offline workflows\n", encoding="utf-8")
        workbook = openpyxl.Workbook()
        workbook.active.append(["tool", "capability"])
        workbook.active.append(["Public Tool", "offline workflows"])
        workbook.save(cls.sources["xlsx"])
        workbook.close()
        cls.empty_text = cls.inputs / "empty.txt"
        cls.empty_text.write_text(" \n", encoding="utf-8")
        textfile = cls.root / "fixture-text.txt"
        textfile.write_text(FACT, encoding="utf-8")
        font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        if not font.is_file():
            raise RuntimeError("synthetic_fixture_font_missing")
        cls.blank_image = cls.inputs / "blank.png"
        for destination, with_text in ((cls.sources["png"], True), (cls.blank_image, False)):
            command = ["ffmpeg", "-v", "error", "-y", "-filter_threads", "1",
                       "-f", "lavfi", "-i", "color=c=white:s=1280x720"]
            if with_text:
                command += ["-vf", f"drawtext=fontfile={font}:textfile={textfile}:fontcolor=black:fontsize=42:x=80:y=120"]
            execute(command + ["-frames:v", "1", "-threads", "1", str(destination)])
        cls.blank_video = cls.inputs / "blank.mp4"
        for image, video in ((cls.sources["png"], cls.sources["mp4"]), (cls.blank_image, cls.blank_video)):
            execute(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(image),
                     "-t", "1", "-r", "2", "-c:v", "libx264", "-threads", "1",
                     "-pix_fmt", "yuv420p", "-an", str(video)])
        with wave.open(str(cls.sources["wav"]), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 16000)

    def check_pipeline(self, case_id: str, source: Path, kind: str, eligible: bool,
                       *, ocr: bool = True) -> None:
        package = hashlib.sha256(case_id.encode()).hexdigest()[:20]
        revision = hashlib.sha256(source.read_bytes()).hexdigest()[:20]
        storage_root = self.root / case_id
        output = storage_root / "99_INBOX/READY_FOR_ANALYSIS" / package
        script = {"text": "text_document_ingest", "document": "text_document_ingest",
                  "spreadsheet": "spreadsheet_ingest", "image": "image_ingest",
                  "video": "ingest_video", "audio": "audio_ingest"}[kind]
        arguments = [sys.executable, str(ROOT / "src" / f"{script}.py"),
                     str(source), "--out", str(output), "--source-id", SOURCE_ID]
        if kind in {"image", "video"} and ocr:
            arguments += ["--ocr", "--ocr-languages", "eng"]
        logs = execute(arguments)
        if kind == "video":
            logs += execute([sys.executable, str(ROOT / "src/evidence_compactor.py"), str(output)])
        self.assertNotIn(NAME_CANARY, logs)
        self.assertNotIn(SOURCE_ID, logs)
        self.assertNotIn(FACT, logs)
        raw = (output / "evidence-summary.json").read_bytes()
        self.assertLessEqual(len(raw), 3072)
        item = {"package_id": package, "revision_key": revision, "kind": kind,
                "evidence_path": expected_evidence_path(package),
                "detail_path": None, "preview_path": None}
        envelope, errors = build_envelope(item, raw)
        self.assertEqual(errors, [])
        self.assertIsNotNone(envelope)
        serialized = json.dumps(envelope)
        self.assertNotIn(SOURCE_ID, serialized)
        self.assertNotIn(NAME_CANARY, serialized)
        self.assertEqual(envelope["evidence_assessment"]["state"], "ELIGIBLE" if eligible else "HELD")
        if eligible:
            text = extracted_text(kind, envelope["semantic_summary"])
            self.assertIn("Public Tool", text)
            self.assertIn("offline", text.lower())
            self.assertIn("workflow", text.lower())
        for path, payload in ((f"{FILE_ROOT}/{package}.json", envelope),
                              (f"{FILE_ROOT}/index.json", build_index([envelope], []))):
            destination = storage_root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(payload), encoding="utf-8")
        response = {"outcome": "CANDIDATES_PROPOSED", "rationale": "Supported workflow tool.",
                    "candidates": [{"title": "Public Tool", "proposed_type": "TECHNOLOGY",
                                    "proposed_status": "TEST", "proposed_domain": "04_AI_AGENTS",
                                    "proposed_category": "INTEGRATIONS", "summary": FACT,
                                    "claims": [FACT]}]}
        fetched = {"status": "OK", "http_status": 200, "excerpt": FACT,
                   "content_sha256": hashlib.sha256(FACT.encode()).hexdigest()}
        with patch("file_evidence_candidate_bridge.fetch_one", return_value=fetched) as fetch, \
             patch("file_evidence_candidate_bridge.call_local_model", return_value=response) as model:
            storage = Storage(root=storage_root)
            first = run(storage, package, 10, URL)
            if eligible:
                self.assertEqual(first, ("created", "candidate_created"))
                self.assertEqual(run(storage, package, 10, URL), ("existing", "already_created"))
                self.assertEqual(model.call_count, 1)
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(len(list(storage_root.rglob("candidate-file-*.md"))), 1)
            else:
                self.assertEqual(first, ("held", "insufficient_file_evidence"))
                self.assertEqual(model.call_count, 0)
                self.assertEqual(fetch.call_count, 0)
                self.assertEqual(list(storage_root.rglob("candidate-file-*.md")), [])
        self.assertFalse((storage_root / "00_LIBRARY").exists())

    def test_text(self):
        self.check_pipeline("text", self.sources["txt"], "text", True)

    def test_pdf(self):
        self.check_pipeline("pdf", self.sources["pdf"], "document", True)

    def test_docx(self):
        self.check_pipeline("docx", self.sources["docx"], "document", True)

    def test_pptx(self):
        self.check_pipeline("pptx", self.sources["pptx"], "document", True)

    def test_csv(self):
        self.check_pipeline("csv", self.sources["csv"], "spreadsheet", True)

    def test_xlsx(self):
        self.check_pipeline("xlsx", self.sources["xlsx"], "spreadsheet", True)

    def test_image_ocr(self):
        self.check_pipeline("image", self.sources["png"], "image", True)

    def test_video_ocr(self):
        self.check_pipeline("video", self.sources["mp4"], "video", True)

    def test_empty_text_held(self):
        self.check_pipeline("empty_text", self.empty_text, "text", False)

    def test_image_without_ocr_held(self):
        self.check_pipeline("no_ocr", self.sources["png"], "image", False, ocr=False)

    def test_blank_video_held(self):
        self.check_pipeline("blank_video", self.blank_video, "video", False)

    def test_audio_without_transcript_held(self):
        self.check_pipeline("audio", self.sources["wav"], "audio", False)


if __name__ == "__main__":
    unittest.main()
