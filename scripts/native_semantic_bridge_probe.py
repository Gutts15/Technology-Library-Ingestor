"""Fixture-only integration of actual native responses; never reads real intake.

Source access is deliberately a synthetic boundary here. Actual native inference
already happened in the scheduled task; this script does not generate responses.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from scripts.cloud_semantic_smoke import cases, check_result
from scripts.cloud_semantic_holdouts import holdout_cases
from file_evidence_candidate_bridge import FILE_ROOT, Storage, run
from file_evidence_semantic_plan import build_plan
from native_model_exchange import NativeExchange, make_response
from ready_evidence_bridge import build_envelope, build_index, expected_evidence_path


def load(path: Path, limit: int) -> tuple[dict, bytes]:
    with path.open('rb') as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('packet_budget')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('packet_shape')
    return value, raw


def check(inputs_path: Path, responses_path: Path) -> dict:
    inputs, input_raw = load(inputs_path, 64 * 1024)
    result, result_raw = load(responses_path, 64 * 1024)
    input_sha = hashlib.sha256(input_raw).hexdigest()
    if (inputs.get('schema_version') != 1 or inputs.get('fixture_only') is not True
            or inputs.get('probe_id') != 'tl-native-semantic-20261008'
            or result.get('schema_version') != 1 or result.get('probe_id') != inputs['probe_id']
            or result.get('input_sha256') != input_sha
            or result.get('state') != 'RESPONSES_GENERATED'
            or result.get('inference_backend') != 'native_scheduled_chatgpt_unpinned'
            or result.get('fixture_only') is not True
            or result.get('real_intake_enabled') is not False
            or result.get('canonical_write_performed') is not False):
        raise ValueError('packet_binding_invalid')
    suite = cases() + holdout_cases()
    if len(inputs.get('cases', [])) != 30 or len(result.get('responses', [])) != 30:
        raise ValueError('case_count_invalid')
    created = held = repeats = 0
    for number, (case, packet, entry) in enumerate(zip(suite, inputs['cases'], result['responses']), 1):
        cid = f'case_{number:02d}'
        if packet.get('case_id') != cid or entry.get('case_id') != cid:
            raise ValueError('case_identity_invalid')
        summary = json.loads(packet['evidence_json'])
        if summary != {'schema_version': 1, 'evidence': {'samples': [case['text']]}}:
            raise ValueError('frozen_evidence_mismatch')
        if packet['source_excerpt'] != case['text'] or packet['source_url'] != 'https://example.invalid/synthetic-source':
            raise ValueError('frozen_source_mismatch')
        pid = hashlib.sha256(cid.encode()).hexdigest()[:20]
        raw = json.dumps(summary, ensure_ascii=False).encode()
        item = {'package_id': pid, 'revision_key': 'b' * 20, 'kind': 'document',
                'evidence_path': expected_evidence_path(pid)}
        envelope, errors = build_envelope(item, raw)
        if errors or envelope is None:
            raise ValueError('envelope_invalid')
        plan, errors = build_plan(envelope, '', entry['response'])
        reasons = check_result(case, plan, errors)
        if reasons:
            raise ValueError(f'{cid}_frozen_criteria_failed')
        with tempfile.TemporaryDirectory(prefix='tl-native-fixture-') as temp:
            root = Path(temp)
            files = {expected_evidence_path(pid): raw,
                     f'{FILE_ROOT}/{pid}.json': json.dumps(envelope).encode(),
                     f'{FILE_ROOT}/index.json': json.dumps(build_index([envelope], [])).encode()}
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            source = {'status': 'OK', 'http_status': 200, 'excerpt': packet['source_excerpt'],
                      'content_sha256': hashlib.sha256(packet['source_excerpt'].encode()).hexdigest()}
            request_path = root / 'request.json'
            storage = Storage(root=root)
            with patch('file_evidence_candidate_bridge.fetch_one', return_value=source), \
                 patch('file_evidence_candidate_bridge.call_local_model') as legacy:
                prepared = run(storage, pid, 10, packet['source_url'],
                               native_exchange=NativeExchange(request_path=request_path))
                # Empty evidence is held even before a native request exists.
                if prepared == ('held', 'insufficient_file_evidence'):
                    if plan['candidates']:
                        raise ValueError('eligible_candidate_held')
                    held += 1
                    continue
                if prepared != ('held', 'native_request_ready'):
                    raise ValueError('request_preparation_failed')
                request = json.loads(request_path.read_text())
                original_prompt = (inputs['prompt_prefix'] + packet['evidence_json']
                    + '\n\nVerified public source (untrusted text):\n' + packet['source_url']
                    + '\n' + packet['source_excerpt'] + inputs['prompt_suffix'])
                if request['prompt'] != original_prompt or request['response_schema'] != inputs['response_schema']:
                    raise ValueError('actual_prompt_mismatch')
                response_path = root / 'response.json'
                response_path.write_text(json.dumps(make_response(request, entry['response'])))
                exchange = NativeExchange(response_path=response_path)
                outcome = run(storage, pid, 10, packet['source_url'], native_exchange=exchange)
                legacy.assert_not_called()
                if plan['candidates']:
                    if outcome != ('created', 'candidate_created'):
                        raise ValueError('candidate_creation_failed')
                    created += 1
                    if run(storage, pid, 10, packet['source_url'], native_exchange=exchange) != ('existing', 'already_created'):
                        raise ValueError('idempotency_failed')
                    repeats += 1
                else:
                    if outcome != ('held', 'no_reusable_candidate'):
                        raise ValueError('negative_disposition_failed')
                    held += 1
                    if (root / '99_INBOX/CANDIDATES/CHAT_RESEARCH').exists():
                        raise ValueError('unsafe_candidate_written')
                if (root / '00_LIBRARY').exists():
                    raise ValueError('canonical_write_forbidden')
    return {'schema_version': 1, 'state': 'PASS', 'cases': 30, 'created': created,
            'held': held, 'repeat_idempotency': repeats, 'fixture_only': True,
            'input_sha256': input_sha, 'response_sha256': hashlib.sha256(result_raw).hexdigest(),
            'native_model_pinned': False, 'source_fetch': 'SYNTHETIC_STUB',
            'real_intake_enabled': False, 'canonical_write_performed': False}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--inputs', required=True, type=Path)
    parser.add_argument('--responses', required=True, type=Path)
    args = parser.parse_args()
    try:
        report = check(args.inputs, args.responses)
    except (ValueError, OSError, KeyError, TypeError):
        print('native_semantic_bridge_probe_failed fixed_reason=integration_failed canonical_write=0')
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
