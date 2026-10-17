"""Contracts for live-model synthetic integration, without claiming live proof."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.cloud_candidate_bridge_smoke import check_case, main, selected_cases
from test_file_evidence_candidate_bridge import model_response


class CloudBridgeSmokeTests(unittest.TestCase):
    def test_five_cases_include_four_useful_and_one_conflict(self):
        suite = selected_cases()
        self.assertEqual(len(suite), 5)
        self.assertEqual(len({case['id'] for case in suite}), 5)
        self.assertEqual(sum('title' in case for case in suite), 4)
        self.assertEqual(suite[2]['id'], 'conflicting')

    def test_real_storage_and_repeat_use_only_one_model_call(self):
        case = selected_cases()[0]
        payload = model_response()
        payload['candidates'][0].update(title=case['title'], summary=case['text'], claims=[case['text']])
        with tempfile.TemporaryDirectory() as temporary, \
             patch('scripts.cloud_candidate_bridge_smoke.live_infer', return_value=payload) as model:
            root = Path(temporary)
            self.assertEqual(check_case(case, root), [])
            model.assert_called_once()
            self.assertEqual(len(list(root.rglob('candidate-file-*.md'))), 1)
            self.assertFalse((root / '00_LIBRARY').exists())

    def test_conflict_is_held_without_a_candidate(self):
        payload = {'outcome': 'NEEDS_REVIEW', 'rationale': 'Conflicting synthetic evidence.', 'candidates': []}
        with tempfile.TemporaryDirectory() as temporary, \
             patch('scripts.cloud_candidate_bridge_smoke.live_infer', return_value=payload):
            root = Path(temporary)
            self.assertEqual(check_case(selected_cases()[2], root), [])
            self.assertEqual(list(root.rglob('candidate-file-*.md')), [])

    def test_canonical_workspace_is_rejected_before_writing(self):
        with patch('sys.argv', ['proof', '--workspace', '00_LIBRARY/synthetic']), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(), 2)
            self.assertIn('canonical_workspace_forbidden', output.getvalue())


if __name__ == '__main__':
    unittest.main()
