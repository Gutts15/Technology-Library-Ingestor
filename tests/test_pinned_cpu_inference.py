"""Real loopback HTTP transport tests with synthetic responses, not model accuracy."""

import io
import json
import threading
import unittest
import sys
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from pinned_cpu_inference import MODEL, MODEL_DIGEST, infer
from candidate_semantic_local import endpoint_is_loopback


class PinnedTransportTests(unittest.TestCase):
    def test_model_pin_matches_the_cloud_quality_proof(self):
        from scripts.cloud_semantic_smoke import MODEL as proof_model, MODEL_DIGEST as proof_digest
        self.assertEqual(MODEL, proof_model)
        self.assertEqual(MODEL_DIGEST, proof_digest)

    def setUp(self):
        self.requests = []
        self.tags = {'models': [{'name': MODEL, 'digest': MODEL_DIGEST}]}
        self.chat = {'model': MODEL, 'done': True,
                     'message': {'content': json.dumps({'synthetic': True})}}
        self.redirect = False
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                test.requests.append((self.path, None))
                if test.redirect:
                    self.send_response(302)
                    self.send_header('Location', test.endpoint + '/redirected')
                    self.end_headers()
                    return
                self.reply(test.tags)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                test.requests.append((self.path, json.loads(raw)))
                self.reply(test.chat)

            def reply(self, payload):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f'http://127.0.0.1:{self.server.server_port}'
        # The existing production guard allows only port 11434. Preserve it;
        # normalize ONLY the isolated fixture port for these real HTTP tests.
        self.guard_patch = patch('pinned_cpu_inference.endpoint_is_loopback',
            side_effect=lambda url: endpoint_is_loopback(
                url.replace(f':{self.server.server_port}', ':11434')))
        self.guard_patch.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.guard_patch.stop()

    def test_production_port_policy_remains_unchanged(self):
        self.assertTrue(endpoint_is_loopback('http://127.0.0.1:11434'))
        self.assertFalse(endpoint_is_loopback('http://127.0.0.1:12345'))
        self.assertFalse(endpoint_is_loopback('https://example.invalid'))

    def test_redirects_are_not_followed(self):
        self.redirect = True
        self.assertIsNone(infer('synthetic_sensitive_marker', {}, 5, endpoint=self.endpoint))
        self.assertEqual(self.requests, [('/api/tags', None)])

    def test_oversized_prompt_is_held_before_network(self):
        self.assertIsNone(infer('x' * (8 * 1024 + 1), {}, 5, endpoint=self.endpoint))
        self.assertEqual(self.requests, [])

    def test_malformed_unicode_prompt_is_held_before_network(self):
        self.assertIsNone(infer('\ud800', {}, 5, endpoint=self.endpoint))
        self.assertEqual(self.requests, [])

    def test_exact_pin_precedes_private_prompt_and_uses_bounded_cpu_settings(self):
        self.assertEqual(infer('synthetic_sensitive_marker', {}, 5, endpoint=self.endpoint),
                         {'synthetic': True})
        self.assertEqual([path for path, body in self.requests], ['/api/tags', '/api/chat'])
        body = self.requests[1][1]
        self.assertEqual(body['model'], MODEL)
        self.assertFalse(body['stream'])
        self.assertFalse(body['think'])
        self.assertEqual(body['options'], {'temperature': 0, 'seed': 0, 'num_ctx': 4096,
                                          'num_predict': 384, 'num_gpu': 0, 'num_thread': 4})
        self.assertEqual(body['messages'][1]['content'], 'synthetic_sensitive_marker')

    def test_wrong_pin_blocks_chat_before_private_content_is_sent(self):
        self.tags['models'][0]['digest'] = '0' * 64
        self.assertIsNone(infer('synthetic_sensitive_marker', {}, 5, endpoint=self.endpoint))
        self.assertEqual(self.requests, [('/api/tags', None)])

    def test_malformed_tag_inventory_blocks_chat(self):
        self.tags = {'models': 'invalid'}
        self.assertIsNone(infer('synthetic_sensitive_marker', {}, 5, endpoint=self.endpoint))
        self.assertEqual(len(self.requests), 1)

    def test_non_loopback_and_invalid_timeouts_make_no_network_request(self):
        for endpoint, timeout in ((self.endpoint, 0), (self.endpoint, float('nan')),
                                  (self.endpoint, float('inf')), ('https://example.invalid', 5)):
            with self.subTest(endpoint=endpoint, timeout=timeout):
                self.assertIsNone(infer('synthetic_sensitive_marker', {}, timeout, endpoint=endpoint))
        self.assertEqual(self.requests, [])

    def test_incomplete_or_different_model_response_is_rejected(self):
        for field, value in (('done', False), ('model', 'different-model')):
            with self.subTest(field=field):
                original = self.chat[field]
                self.chat[field] = value
                self.assertIsNone(infer('synthetic_sensitive_marker', {}, 5, endpoint=self.endpoint))
                self.chat[field] = original

    def test_token_limit_response_is_held_even_when_prefix_is_valid_json(self):
        self.chat['done_reason'] = 'length'
        with redirect_stdout(io.StringIO()) as output:
            self.assertIsNone(infer('synthetic_sensitive_marker', {}, 5, endpoint=self.endpoint))
        self.assertEqual(output.getvalue(), '')

    def test_oversized_or_invalid_payload_is_rejected_without_output(self):
        for content in ('x' * (64 * 1024 + 1), 'invalid JSON', '[]'):
            self.chat['message']['content'] = content
            with self.subTest(size=len(content)), redirect_stdout(io.StringIO()) as output:
                self.assertIsNone(infer('synthetic_sensitive_marker', {}, 5, endpoint=self.endpoint))
                self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
