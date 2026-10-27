"""E2 one-shot smoke: synthetic mocks only, never call real Gemini/Drive."""
from __future__ import annotations

import hashlib
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import e2_synthetic_gemini_smoke as smoke


class FakeStorage:
    def __init__(self):
        self.input = b'{"synthetic":true}'
        self.files = {}
        self.calls = 0

    def read_input(self):
        return self.input

    def entries(self, name):
        return 1 if name in self.files else 0

    def read(self, name):
        return self.files[name]

    def command(self, args, write=False):
        self.calls += 1
        if args[0] != "copyto" or not write or "--immutable" not in args:
            raise AssertionError("write must be immutable")
        name = args[2].split("/")[-1]
        if name in self.files:
            raise AssertionError("would overwrite")
        self.files[name] = Path(args[1]).read_bytes()
        return b""


class E2SyntheticSmokeTests(unittest.TestCase):
    def setUp(self):
        self.storage = FakeStorage()
        self.now = datetime(2026, 10, 27, 8, 20, tzinfo=timezone.utc)
        self.sha_patch = patch.object(
            smoke, "INPUT_SHA256", hashlib.sha256(self.storage.input).hexdigest())
        self.len_patch = patch.object(smoke, "INPUT_BYTES", len(self.storage.input))
        self.sha_patch.start()
        self.len_patch.start()
        self.addCleanup(self.sha_patch.stop)
        self.addCleanup(self.len_patch.stop)

    def response(self, item, action="HOLD", reason="WEAK_EVIDENCE"):
        return {"decisoes": [{
            "item_id": item["item_id"], "acao": action, "motivo": reason,
            "confianca": 0.8, "record_type": None, "route": None,
            "target_record_id": None, "record_markdown": None, "claims": [],
        }]}

    def test_writes_exactly_one_metadata_receipt(self):
        result = smoke.run_probe(self.storage, model_call=self.response, now=self.now)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["action"], "HOLD")
        self.assertEqual(self.storage.calls, 1)
        receipt = json.loads(next(iter(self.storage.files.values())))
        self.assertNotIn("evidence_compact", receipt)
        self.assertNotIn("record_markdown", receipt)
        self.assertNotIn("decisoes", receipt)
        self.assertEqual(receipt["fixture_sha256"], smoke.INPUT_SHA256)

    def test_repeat_does_not_call_model_or_write(self):
        smoke.run_probe(self.storage, model_call=self.response, now=self.now)
        result = smoke.run_probe(self.storage,
                                 model_call=lambda _: self.fail("model replay"), now=self.now)
        self.assertEqual(result["status"], "ALREADY_EXISTS")
        self.assertEqual(self.storage.calls, 1)

    def test_invalid_action_rejected_before_any_write(self):
        with self.assertRaisesRegex(smoke.ProbeError, "MODEL_CONTRACT_INVALID"):
            smoke.run_probe(self.storage,
                            model_call=lambda item: self.response(item, action="DELETE"),
                            now=self.now)
        self.assertEqual(self.storage.calls, 0)

    def test_unexpected_write_intent_is_blocked(self):
        with self.assertRaisesRegex(smoke.ProbeError, "UNSAFE_SYNTHETIC_DECISION"):
            smoke.run_probe(self.storage,
                            model_call=lambda item: self.response(item, action="UNCHANGED",
                                                                  reason="NO_NEW_INFORMATION"),
                            now=self.now)
        self.assertEqual(self.storage.calls, 0)

    def test_fixture_hash_mismatch_fails_closed(self):
        self.storage.input = b'{"private":"not approved"}'
        with self.assertRaisesRegex(smoke.ProbeError, "FIXTURE_MISMATCH"):
            smoke.run_probe(self.storage, model_call=self.response, now=self.now)
        self.assertEqual(self.storage.calls, 0)

    def test_existing_receipt_mismatch_blocks_without_model(self):
        name = smoke.receipt_name("2026-10-27")
        self.storage.files[name] = b'{"status":"PASS"}'
        with self.assertRaisesRegex(smoke.ProbeError, "EXISTING_RECEIPT_INVALID"):
            smoke.run_probe(self.storage, model_call=lambda _: self.fail("no replay"), now=self.now)
        self.assertEqual(self.storage.calls, 0)


if __name__ == "__main__":
    unittest.main()
