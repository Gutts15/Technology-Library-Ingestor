"""Native response transport must preserve the actual candidate boundary."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from file_evidence_candidate_bridge import Storage, run
from native_model_exchange import NativeExchange, make_response
from tests.test_file_evidence_candidate_bridge import PACKAGE, fixture, fetched_source, model_response


class NativeExchangeTests(unittest.TestCase):
    def prepare(self, root):
        fixture(root, kind='document')
        request = root / 'native-request.json'
        with patch('file_evidence_candidate_bridge.fetch_one', side_effect=fetched_source), \
             patch('file_evidence_candidate_bridge.call_local_model') as local:
            self.assertEqual(run(Storage(root=root), PACKAGE, 8,
                                 native_exchange=NativeExchange(request_path=request)),
                             ('held', 'native_request_ready'))
            local.assert_not_called()
        self.assertFalse((root / '99_INBOX/CANDIDATES/CHAT_RESEARCH').exists())
        return json.loads(request.read_text())

    def apply_response(self, root, response, source=fetched_source):
        path = root / 'native-response.json'
        path.write_text(json.dumps(response))
        with patch('file_evidence_candidate_bridge.fetch_one', side_effect=source), \
             patch('file_evidence_candidate_bridge.call_local_model') as local, \
             patch('file_evidence_candidate_bridge.infer_pinned_cpu') as cpu:
            result = run(Storage(root=root), PACKAGE, 8,
                         native_exchange=NativeExchange(response_path=path))
            local.assert_not_called()
            cpu.assert_not_called()
        return result

    def test_matching_response_creates_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = self.prepare(root)
            response = make_response(request, model_response())
            self.assertEqual(self.apply_response(root, response), ('created', 'candidate_created'))
            with patch('file_evidence_candidate_bridge.fetch_one') as fetch:
                self.assertEqual(run(Storage(root=root), PACKAGE, 8,
                                     native_exchange=NativeExchange(response_path=root/'missing.json')),
                                 ('existing', 'already_created'))
                fetch.assert_not_called()
            self.assertFalse((root / '00_LIBRARY').exists())

    def test_wrong_request_and_source_revision_hold(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = self.prepare(root)
            response = make_response(request, model_response())
            wrong = copy.deepcopy(response)
            wrong['request_sha256'] = 'f' * 64
            self.assertEqual(self.apply_response(root, wrong), ('held', 'native_response_binding_mismatch'))
            def changed_source(*args):
                return {**fetched_source(), 'content_sha256': 'd' * 64}
            self.assertEqual(self.apply_response(root, response, changed_source),
                             ('held', 'native_response_binding_mismatch'))
            self.assertFalse((root / '99_INBOX/CANDIDATES/CHAT_RESEARCH').exists())

    def test_invalid_model_and_invented_subject_hold(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = self.prepare(root)
            bad = make_response(request, {'outcome': 'CANDIDATES_PROPOSED'})
            self.assertTrue(self.apply_response(root, bad)[1].startswith('model_contract_'))
            invented = model_response()
            invented['candidates'][0]['title'] = 'Imaginary Product'
            self.assertEqual(self.apply_response(root, make_response(request, invented)),
                             ('held', 'subject_not_in_public_source'))

    def test_response_fields_and_size_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = self.prepare(root)
            response = make_response(request, model_response())
            response['extra'] = 'untrusted'
            self.assertEqual(self.apply_response(root, response), ('held', 'native_response_invalid'))
            response = make_response(request, model_response())
            response['response']['rationale'] = 'x' * 100000
            self.assertEqual(self.apply_response(root, response), ('held', 'native_response_invalid'))

    def test_no_evidence_never_requests_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root, kind='video', evidence={'ocr': [], 'speech': []})
            request = root / 'request.json'
            with patch('file_evidence_candidate_bridge.fetch_one') as fetch:
                self.assertEqual(run(Storage(root=root), PACKAGE, 8,
                                     native_exchange=NativeExchange(request_path=request)),
                                 ('held', 'insufficient_file_evidence'))
                fetch.assert_not_called()
            self.assertFalse(request.exists())

    def test_prepare_never_overwrites_different_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare(root)
            path = root / 'native-request.json'
            path.write_bytes(b'existing')
            with patch('file_evidence_candidate_bridge.fetch_one', side_effect=fetched_source):
                self.assertEqual(run(Storage(root=root), PACKAGE, 8,
                                     native_exchange=NativeExchange(request_path=path)),
                                 ('held', 'native_request_conflict'))
            self.assertEqual(path.read_bytes(), b'existing')

    def test_duplicate_keys_and_non_json_constants_hold(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare(root)
            path = root / 'response.json'
            for content in ('{"response":{},"response":{}}', '{"response":NaN}'):
                path.write_text(content)
                with patch('file_evidence_candidate_bridge.fetch_one', side_effect=fetched_source):
                    self.assertEqual(run(Storage(root=root), PACKAGE, 8,
                                         native_exchange=NativeExchange(response_path=path)),
                                     ('held', 'native_response_invalid'))


if __name__ == '__main__':
    unittest.main()
