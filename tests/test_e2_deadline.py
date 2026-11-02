"""Real subprocess deadline, including partial output; no network or API key."""
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import e2_gemini_decide as e2
from e1_lote_prepare import encode
from tests import test_e2_gemini_decide as fixtures


class ModelDeadlineTests(unittest.TestCase):
    def test_no_response_and_partial_response_are_bounded(self):
        real_run = subprocess.run
        for partial in (False, True):
            with self.subTest(partial=partial):
                script = ("import sys,time; " +
                          ("sys.stdout.write('{');sys.stdout.flush();" if partial else "") +
                          "time.sleep(30)")
                def blocked(*args, **kwargs):
                    return real_run([sys.executable, "-c", script], **kwargs)
                started = time.monotonic()
                with patch.object(e2.subprocess, "run", side_effect=blocked):
                    with self.assertRaisesRegex(e2.DecisionError, "model_deadline_exceeded"):
                        e2.ask_gemini({}, "synthetic-placeholder", timeout=0.15)
                self.assertLess(time.monotonic() - started, 3)

    def test_worker_failure_never_exposes_provider_body(self):
        result = subprocess.CompletedProcess([], 2, b'{"error":"model_quota_blocked"}',
                                             b'private-provider-body')
        with patch.object(e2.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(e2.DecisionError, '^model_quota_blocked$'):
                e2.ask_gemini({}, 'synthetic-placeholder')
        result.stdout = b'private-provider-body'
        with patch.object(e2.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(e2.DecisionError, '^model_worker_failed$'):
                e2.ask_gemini({}, 'synthetic-placeholder')

    def test_total_budget_preserves_lot_without_partial_decisions(self):
        case = fixtures.TestE2()
        case.setUp()
        self.addCleanup(case.doCleanups)
        lot, path = case.fixture()
        second = dict(lot['itens'][0])
        import hashlib
        second['package_id'] = '3' * 20
        second['item_id'] = hashlib.sha256(('e1|' + second['package_id'] + '|' +
            second['revision_key'] + '|' + second['evidence_sha256']).encode()).hexdigest()[:20]
        lot['itens'].append(second)
        lot['items_sha256'] = hashlib.sha256(encode(lot['itens'])).hexdigest()
        (case.root / path).write_bytes(encode(lot))
        answer = {'decisoes': [case.decision(lot['itens'][0]['item_id'])]}
        with patch.object(e2.time, 'monotonic', side_effect=[0, 0, 2]), \
             patch.object(e2, 'ask_gemini', return_value=answer):
            with self.assertRaisesRegex(e2.DecisionError, 'model_cycle_budget_exceeded'):
                e2.decide(case.store, api_key='synthetic-placeholder', apply=True, max_seconds=1)
        self.assertEqual((case.root / path).read_bytes(), encode(lot))
        self.assertFalse((case.root / e2.LOTES / '2026-10-27/decisoes.json').exists())


if __name__ == '__main__':
    unittest.main()
