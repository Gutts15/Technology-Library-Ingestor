"""Acceptance expectations defined before running the independent holdouts."""

import io
import hashlib
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts.cloud_semantic_holdouts import holdout_cases
from scripts.cloud_semantic_smoke import check_result, main


class HoldoutContractTests(unittest.TestCase):
    def test_acceptance_inputs_and_expectations_remain_frozen(self):
        raw = json.dumps(holdout_cases(), sort_keys=True, ensure_ascii=False,
                         separators=(',', ':')).encode()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         '8b3a3dcde79143669acd159783c0bbc5bbada727d4d97543f392d001bd6fce69')

    def test_twenty_unique_cases_are_balanced_and_serially_bounded(self):
        suite = holdout_cases()
        self.assertEqual(len(suite), 20)
        self.assertEqual(len({case['id'] for case in suite}), 20)
        self.assertEqual(sum('title' in case for case in suite), 10)
        self.assertEqual([len(suite[start:start + 5]) for start in (0, 5, 10, 15)], [5] * 4)
        self.assertTrue(all(case.get('required_claim_tokens') for case in suite if 'title' in case))

    def test_relevance_alone_does_not_pass_without_core_claim(self):
        case = holdout_cases()[0]
        plan = {'outcome': 'CANDIDATES_PROPOSED', 'candidates': [
            {'title': case['title'], 'summary': 'A useful library.', 'claims': ['It is software.']}]}
        self.assertIn('missing_core_claim', check_result(case, plan, []))

    def test_invented_capability_in_summary_or_claims_cannot_pass(self):
        case = holdout_cases()[0]
        for field in ('summary', 'claims'):
            candidate = {'title': case['title'], 'summary': 'Streaming JSON Lines parser.',
                         'claims': ['It parses JSON Lines as a stream.']}
            candidate[field] = 'It integrates PostgreSQL.' if field == 'summary' else [
                *candidate['claims'], 'It integrates PostgreSQL.']
            plan = {'outcome': 'CANDIDATES_PROPOSED', 'candidates': [candidate]}
            with self.subTest(field=field):
                self.assertIn('unsupported_claim', check_result(case, plan, []))

    def test_required_claim_tokens_can_be_paraphrased_bilingually(self):
        case = holdout_cases()[0]
        plan = {'outcome': 'CANDIDATES_PROPOSED', 'candidates': [
            {'title': case['title'], 'summary': 'Biblioteca de análise.',
             'claims': ['Analisa JSON Lines em fluxo.']}]}
        self.assertEqual(check_result(case, plan, []), [])

    def test_negative_holdouts_reject_any_candidate(self):
        for case in holdout_cases():
            if 'title' not in case:
                plan = {'outcome': case['outcomes'][0], 'candidates': [{'title': 'InventedTool'}]}
                with self.subTest(case=case['id']):
                    self.assertIn('unsafe_candidate', check_result(case, plan, []))

    def test_holdout_window_overflow_is_rejected_before_network(self):
        with patch('sys.argv', ['proof', '--suite', 'holdouts', '--start', '19', '--count', '2']), \
             patch('scripts.cloud_semantic_smoke.request_json') as network, \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(), 2)
            self.assertIn('invalid_case_window', output.getvalue())
            network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
