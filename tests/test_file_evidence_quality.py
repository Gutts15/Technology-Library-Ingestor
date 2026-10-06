"""Synthetic checks at the automatic candidate boundary."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from file_evidence_candidate_bridge import Storage, run
from evidence_quality import assess_evidence
from test_file_evidence_candidate_bridge import PACKAGE, URL, fetched_source, fixture, model_response


class EvidenceQualityTests(unittest.TestCase):
    def test_empty_extracted_content_never_calls_network_or_model(self):
        for kind, evidence in (
            ("image", {"ocr_sample": ""}),
            ("audio", {"speech": []}),
            ("document", {"samples": [" "]}),
            ("spreadsheet", {"row_samples": [["", " "]]}),
            ("unknown", {"samples": ["Synthetic unsupported content"]}),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fixture(root, kind=kind, evidence=evidence)
                with patch("file_evidence_candidate_bridge.fetch_one") as fetch, \
                     patch("file_evidence_candidate_bridge.call_local_model") as model:
                    self.assertEqual(run(Storage(root=root), PACKAGE, 8, URL),
                                     ("held", "insufficient_file_evidence"))
                    fetch.assert_not_called()
                    model.assert_not_called()
                self.assertFalse((root / "99_INBOX/CANDIDATES/CHAT_RESEARCH").exists())

    def test_usable_text_still_creates_candidate(self):
        for kind, evidence in (
            ("image", {"ocr_sample": "Public Tool is a workflow engine."}),
            ("document", {"samples": ["Public Tool is a workflow engine."]}),
            ("spreadsheet", {"row_samples": [["Public Tool", "workflow engine"]]}),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fixture(root, kind=kind, evidence=evidence)
                with patch("file_evidence_candidate_bridge.fetch_one", side_effect=fetched_source), \
                     patch("file_evidence_candidate_bridge.call_local_model", return_value=model_response()):
                    self.assertEqual(run(Storage(root=root), PACKAGE, 8, URL),
                                     ("created", "candidate_created"))
                self.assertFalse((root / "00_LIBRARY").exists())

    def test_weak_or_unknown_audio_is_held(self):
        for signal in ("low", "unknown", None, ["high"]):
            with self.subTest(signal=signal), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fixture(root, kind="audio", evidence={"speech": [{"text": "Public Tool"}]},
                        quality={"transcript": {"signal": signal}})
                with patch("file_evidence_candidate_bridge.fetch_one") as fetch, \
                     patch("file_evidence_candidate_bridge.call_local_model") as model:
                    self.assertEqual(run(Storage(root=root), PACKAGE, 8, URL),
                                     ("held", "insufficient_file_evidence"))
                    fetch.assert_not_called()
                    model.assert_not_called()

    def test_usable_audio_is_eligible(self):
        for signal in ("medium", "high"):
            with self.subTest(signal=signal), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fixture(root, kind="audio", evidence={"speech": [{"text": "Public Tool"}]},
                        quality={"transcript": {"signal": signal}})
                with patch("file_evidence_candidate_bridge.fetch_one", side_effect=fetched_source), \
                     patch("file_evidence_candidate_bridge.call_local_model", return_value=model_response()):
                    self.assertEqual(run(Storage(root=root), PACKAGE, 8, URL),
                                     ("created", "candidate_created"))

    def test_ocr_can_support_video_without_using_weak_speech(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root, kind="video",
                    evidence={"ocr": [{"text": "Public Tool"}],
                              "speech": [{"text": "WEAK_TRANSCRIPT_SENTINEL"}]},
                    quality={"transcript": {"signal": "low"}})
            with patch("file_evidence_candidate_bridge.fetch_one", side_effect=fetched_source), \
                 patch("file_evidence_candidate_bridge.call_local_model", return_value=model_response()) as model:
                self.assertEqual(run(Storage(root=root), PACKAGE, 8, URL), ("created", "candidate_created"))
                self.assertNotIn("WEAK_TRANSCRIPT_SENTINEL", model.call_args.args[0])

    def test_assessment_is_derived_from_bound_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, envelope = fixture(root, kind="image", evidence={"ocr_sample": ""})
            assessment = envelope["evidence_assessment"]
            self.assertEqual(assessment["state"], "HELD")
            self.assertEqual(assessment["reason_codes"], ["no_extracted_evidence"])
            assessment["state"] = "ELIGIBLE"
            path = root / "99_INBOX/CANDIDATES/FILE_EVIDENCE" / f"{PACKAGE}.json"
            path.write_text(json.dumps(envelope), encoding="utf-8")
            with patch("file_evidence_candidate_bridge.fetch_one") as fetch:
                self.assertEqual(run(Storage(root=root), PACKAGE, 8, URL),
                                 ("held", "evidence_assessment_mismatch"))
                fetch.assert_not_called()

    def test_quality_warning_overrides_high_signal(self):
        assessment = assess_evidence("audio", {
            "quality": {"transcript": {"signal": "high"}},
            "evidence": {"speech": [{"text": "Synthetic speech"}]},
            "warnings": ["transcript_low_quality_signal"],
        })
        self.assertEqual(assessment["state"], "HELD")
        self.assertFalse(assessment["semantic_correctness_verified"])

    def test_malformed_channel_does_not_count_as_extracted_text(self):
        for kind, evidence in (("image", {"ocr_sample": {"text": "Synthetic"}}),
                               ("video", {"ocr": [{"text": ["Synthetic"]}]}),
                               ("document", {"samples": {"text": "Synthetic"}})):
            with self.subTest(kind=kind):
                self.assertEqual(assess_evidence(kind, {"evidence": evidence})["state"], "HELD")

    def test_legacy_envelope_is_reassessed_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, envelope = fixture(root)
            del envelope["evidence_assessment"]
            path = root / "99_INBOX/CANDIDATES/FILE_EVIDENCE" / f"{PACKAGE}.json"
            original = json.dumps(envelope).encode()
            path.write_bytes(original)
            with patch("file_evidence_candidate_bridge.fetch_one", side_effect=fetched_source), \
                 patch("file_evidence_candidate_bridge.call_local_model", return_value=model_response()):
                self.assertEqual(run(Storage(root=root), PACKAGE, 8, URL), ("created", "candidate_created"))
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
