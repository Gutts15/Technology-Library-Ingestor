"""Opt-in integration contracts; model accuracy is proven separately in the cloud."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from file_evidence_candidate_bridge import Storage, run
from test_file_evidence_candidate_bridge import PACKAGE, URL, fixture, fetched_source, model_response


class PinnedCandidateBridgeTests(unittest.TestCase):
    def test_opt_in_uses_pinned_cpu_once_and_preserves_idempotency(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root)
            with patch('file_evidence_candidate_bridge.fetch_one', side_effect=fetched_source), \
                 patch('file_evidence_candidate_bridge.infer_pinned_cpu', return_value=model_response()) as cpu, \
                 patch('file_evidence_candidate_bridge.call_local_model') as legacy:
                self.assertEqual(run(Storage(root=root), PACKAGE, 10, URL, pinned_cpu=True),
                                 ('created', 'candidate_created'))
                self.assertEqual(run(Storage(root=root), PACKAGE, 10, URL, pinned_cpu=True),
                                 ('existing', 'already_created'))
                cpu.assert_called_once()
                legacy.assert_not_called()
            self.assertFalse((root / '00_LIBRARY').exists())

    def test_pin_failure_never_falls_back_or_writes_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root)
            with patch('file_evidence_candidate_bridge.fetch_one', side_effect=fetched_source), \
                 patch('file_evidence_candidate_bridge.infer_pinned_cpu', return_value=None), \
                 patch('file_evidence_candidate_bridge.call_local_model') as legacy:
                outcome, reason = run(Storage(root=root), PACKAGE, 10, URL, pinned_cpu=True)
                self.assertEqual(outcome, 'held')
                self.assertTrue(reason.startswith('model_contract_'))
                legacy.assert_not_called()
            self.assertEqual(list(root.rglob('candidate-file-*.md')), [])
            self.assertFalse((root / '00_LIBRARY').exists())

    def test_weak_evidence_stops_before_cpu_or_source_fetch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root, kind='image', evidence={'ocr_sample': ''})
            with patch('file_evidence_candidate_bridge.fetch_one') as fetch, \
                 patch('file_evidence_candidate_bridge.infer_pinned_cpu') as cpu:
                self.assertEqual(run(Storage(root=root), PACKAGE, 10, URL, pinned_cpu=True),
                                 ('held', 'insufficient_file_evidence'))
                fetch.assert_not_called()
                cpu.assert_not_called()

    def test_default_path_does_not_silently_switch_models(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root)
            with patch('file_evidence_candidate_bridge.fetch_one', side_effect=fetched_source), \
                 patch('file_evidence_candidate_bridge.infer_pinned_cpu') as cpu, \
                 patch('file_evidence_candidate_bridge.call_local_model', return_value=model_response()) as legacy:
                self.assertEqual(run(Storage(root=root), PACKAGE, 10, URL), ('created', 'candidate_created'))
                legacy.assert_called_once()
                cpu.assert_not_called()


if __name__ == '__main__':
    unittest.main()
