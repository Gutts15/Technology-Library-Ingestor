"""G0.3b boundary tests: no network, secrets, or private Drive fixtures."""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


class G03BTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.import_module("scripts.g03b_gemini_receipt")

    def setUp(self):
        self.m = self.mod
        self.raw = b'{"synthetic":"only"}'
        self.sha = hashlib.sha256(self.raw).hexdigest()
        self.clock = lambda: datetime(2026, 10, 21, 12, 7, tzinfo=timezone.utc)
        self.response_text = '{"gate":"G0.3b","status":"OK"}'
        self.response = json.dumps({
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": self.response_text}]}}],
            "modelVersion": self.m.MODEL,
            "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 12, "totalTokenCount": 42},
        }).encode()

    def probe(self, storage=None, response=None, event="schedule", clock=None):
        storage = storage or MemoryStorage(self.raw)
        calls = []
        def generate(raw):
            calls.append(raw)
            if isinstance(response, Exception):
                raise response
            return self.response if response is None else response
        result = self.m.run_probe(storage, generate, event=event, clock=clock or self.clock,
                                  expected_sha256=self.sha, expected_input_bytes=len(self.raw))
        return result, storage, calls

    def test_module_exists(self):
        self.assertTrue(callable(self.m.run_probe))

    def test_new_receipt_binds_input_model_time_and_real_response_size(self):
        result, storage, calls = self.probe()
        self.assertEqual("PASS", result["status"])
        self.assertEqual([self.raw], calls)
        self.assertEqual(1, storage.writes)
        receipt = json.loads(storage.receipts[self.m.receipt_name("2026-10-21")])
        self.assertEqual(self.sha, receipt["input_sha256"])
        self.assertEqual(self.m.MODEL, receipt["model"])
        self.assertEqual("2026-10-21", receipt["date_brt"])
        self.assertEqual("2026-10-21T09:07:00-03:00", receipt["executed_at_brt"])
        self.assertEqual(len(self.response_text.encode()), receipt["response_bytes"])
        self.assertEqual("schedule", receipt["event_name"])
        self.assertEqual(42, receipt["usage_tokens"]["totalTokenCount"])
        self.assertEqual(storage.receipts[self.m.receipt_name("2026-10-21")], storage.last_read)

    def test_repeat_reads_existing_receipt_without_model_or_write(self):
        _, storage, _ = self.probe()
        before = dict(storage.receipts)
        result, _, calls = self.probe(storage)
        self.assertEqual("ALREADY_EXISTS", result["status"])
        self.assertEqual([], calls)
        self.assertEqual(before, storage.receipts)
        self.assertEqual(1, storage.writes)

    def test_manual_receipt_never_counts_as_scheduled_day_on_repeat(self):
        _, storage, _ = self.probe(event="workflow_dispatch")
        result, _, calls = self.probe(storage)
        self.assertEqual(0, result["scheduled_eligible"])
        self.assertEqual([], calls)
        self.assertEqual(1, storage.writes)

    def test_wrong_input_hash_stops_before_model_or_write(self):
        storage = MemoryStorage(b"changed input")
        with self.assertRaises(self.m.ProbeError):
            self.probe(storage)
        self.assertEqual(0, storage.writes)

    def test_invalid_response_never_writes(self):
        bad_responses = [b"not json", b"{}", self.response.replace(b"STOP", b"MAX_TOKENS"),
                         self.response.replace(b"OK", b"NO"), self.response.replace(b"G0.3b", b"wrong"),
                         self.response.replace(b'\\"status\\":\\"OK\\"', b'\\"status\\":\\"OK\\",\\"extra\\":true'),
                         b"x" * 65537]
        for raw in bad_responses:
            storage = MemoryStorage(self.raw)
            with self.subTest(size=len(raw)), self.assertRaises(self.m.ProbeError):
                self.probe(storage, raw)
            self.assertEqual(0, storage.writes)

    def test_valid_json_with_provider_thought_signature_is_accepted(self):
        body = json.loads(self.response)
        body['candidates'][0]['content']['parts'][0]['thoughtSignature'] = 'opaque-signature'
        result, storage, _ = self.probe(response=json.dumps(body).encode())
        self.assertEqual('PASS', result['status'])
        self.assertEqual(1, storage.writes)

    def test_quota_and_billing_errors_never_write_or_retry_model(self):
        for code in ("MODEL_QUOTA_BLOCKED", "MODEL_PAYMENT_BLOCKED", "MODEL_ACCESS_BLOCKED"):
            storage = MemoryStorage(self.raw)
            with self.assertRaises(self.m.ProbeError):
                self.probe(storage, self.m.ProbeError(code))
            self.assertEqual(0, storage.writes)

    def test_readback_mismatch_fails_without_second_write(self):
        storage = MemoryStorage(self.raw)
        storage.corrupt = True
        with self.assertRaises(self.m.ProbeError):
            self.probe(storage)
        self.assertEqual(1, storage.writes)

    def test_uncertain_upload_reconciles_exact_bytes_without_reupload(self):
        storage = MemoryStorage(self.raw)
        storage.uncertain = True
        result, _, _ = self.probe(storage)
        self.assertEqual("PASS", result["status"])
        self.assertEqual(1, storage.writes)

    def test_uncertain_upload_without_receipt_fails_without_reupload(self):
        storage = MemoryStorage(self.raw)
        storage.uncertain = True
        storage.disappear = True
        with self.assertRaises(self.m.ProbeError):
            self.probe(storage)
        self.assertEqual(1, storage.writes)

    def test_preexisting_invalid_or_duplicate_receipt_is_never_overwritten(self):
        for duplicate in (False, True):
            storage = MemoryStorage(self.raw)
            storage.receipts[self.m.receipt_name("2026-10-21")] = b"invalid"
            storage.duplicate = duplicate
            with self.assertRaises(self.m.ProbeError):
                self.probe(storage)
            self.assertEqual(0, storage.writes)

    def test_midnight_boundary_fails_before_write(self):
        moments = iter([datetime(2026, 10, 21, 2, 59, 59, tzinfo=timezone.utc),
                        datetime(2026, 10, 21, 3, 0, 1, tzinfo=timezone.utc)])
        storage = MemoryStorage(self.raw)
        with self.assertRaises(self.m.ProbeError):
            self.probe(storage, clock=lambda: next(moments))
        self.assertEqual(0, storage.writes)

    def test_unexpected_trigger_is_rejected(self):
        storage = MemoryStorage(self.raw)
        with self.assertRaises(self.m.ProbeError):
            self.probe(storage, event="pull_request")
        self.assertEqual(0, storage.writes)

    def test_missing_secret_stops_before_network_and_logs_no_value(self):
        out = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(out):
            self.assertEqual(1, self.m.main())
        self.assertEqual("g03b status=MISSING_SECRET receipts_written=0\n", out.getvalue())

    def test_rclone_command_is_bounded_silent_and_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.m.RcloneStorage(Path(tmp) / "config", Path(tmp))
            recorded = []
            def invoke(args, **kwargs):
                recorded.append((args, kwargs))
                return type("Result", (), {"returncode": 0, "stdout": b""})()
            with patch("subprocess.run", side_effect=invoke):
                store.create(self.m.receipt_name("2026-10-21"), b"{}")
            args, kwargs = recorded[0]
            self.assertIn("--immutable", args)
            self.assertIn("--ignore-existing", args)
            self.assertEqual("1", args[args.index("--retries") + 1])
            self.assertEqual("1", args[args.index("--low-level-retries") + 1])
            self.assertNotIn("shell", kwargs)
            self.assertLessEqual(kwargs["timeout"], 120)
            self.assertEqual("ERROR", args[args.index("--log-level") + 1])

    def test_gemini_key_goes_only_in_header_and_request_is_fixed(self):
        captured = []
        class Reply:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, count): return self_raw
        self_raw = self.response
        class Client:
            def open(self, request, timeout):
                captured.append((request, timeout))
                return Reply()
        with patch("urllib.request.build_opener", return_value=Client()):
            self.assertEqual(self.response, self.m.generate(self.raw, "synthetic-key"))
        req, timeout = captured[0]
        self.assertNotIn("synthetic-key", req.full_url)
        self.assertEqual("synthetic-key", req.get_header("X-goog-api-key"))
        body = json.loads(req.data)
        self.assertEqual(self.m.MODEL, req.full_url.split("/models/")[1].split(":")[0])
        self.assertNotIn("tools", body)
        self.assertEqual(128, body["generationConfig"]["maxOutputTokens"])
        self.assertIn(self.raw.decode(), body["contents"][0]["parts"][0]["text"])
        self.assertLessEqual(timeout, 60)


class MemoryStorage:
    def __init__(self, raw):
        self.raw = raw
        self.receipts = {}
        self.writes = 0
        self.corrupt = self.uncertain = self.disappear = self.duplicate = False
        self.last_read = None
    def read_input(self): return self.raw
    def entries(self, name):
        count = 1 if name in self.receipts else 0
        return count + int(self.duplicate and count > 0)
    def read(self, name):
        self.last_read = self.receipts[name]
        return b"corrupt" if self.corrupt else self.last_read
    def create(self, name, content):
        self.writes += 1
        if not self.disappear:
            self.receipts[name] = content
        if self.uncertain:
            raise importlib.import_module("scripts.g03b_gemini_receipt").ProbeError("UPLOAD_UNCERTAIN")


if __name__ == "__main__":
    unittest.main()
