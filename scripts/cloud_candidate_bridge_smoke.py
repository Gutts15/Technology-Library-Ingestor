"""Five synthetic cases through the actual opt-in candidate bridge.

Live model transport; fixture-only verified-source probe; ephemeral local storage.
No Drive, public publication, real source fetch or raw response printing.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))

from file_evidence_candidate_bridge import Storage, run
from file_evidence_semantic_plan import build_plan
from pinned_cpu_inference import infer as live_infer
from scripts.cloud_semantic_smoke import MODEL, cases, check_result, request_json
from test_file_evidence_candidate_bridge import PACKAGE, URL, fixture


def selected_cases():
    suite = cases()
    return [suite[index] for index in (0, 1, 3, 5, 6)]


def check_case(case, root: Path):
    _, envelope = fixture(root, kind='document', evidence={'samples': [case['text']]})
    captured = []

    def model(prompt, schema, timeout):
        payload = live_infer(prompt, schema, timeout)
        captured.append(payload)
        return payload

    def source(*args):
        return {'status': 'OK', 'http_status': 200, 'excerpt': case['text'],
                'content_sha256': hashlib.sha256(case['text'].encode()).hexdigest()}

    with patch('file_evidence_candidate_bridge.fetch_one', side_effect=source), \
         patch('file_evidence_candidate_bridge.infer_pinned_cpu', side_effect=model):
        first = run(Storage(root=root), PACKAGE, 90, URL, pinned_cpu=True)
        calls_after_first = len(captured)
        if not captured:
            return ['model_not_called']
        plan, errors = build_plan(envelope, '', captured[0])
        reasons = check_result(case, plan, errors)
        if 'title' in case:
            if first != ('created', 'candidate_created'):
                reasons.append('candidate_not_created')
            elif run(Storage(root=root), PACKAGE, 90, URL, pinned_cpu=True) != ('existing', 'already_created'):
                reasons.append('repeat_not_idempotent')
            if len(captured) != calls_after_first:
                reasons.append('duplicate_inference')
            if len(list(root.rglob('candidate-file-*.md'))) != 1:
                reasons.append('candidate_storage_count')
        elif first[0] != 'held' or list(root.rglob('candidate-file-*.md')):
            reasons.append('unsafe_candidate_write')
    if (root / '00_LIBRARY').exists():
        reasons.append('canonical_write')
    return reasons


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True, type=Path)
    args = parser.parse_args()
    if any(part.upper() == '00_LIBRARY' for part in args.workspace.parts):
        print('cloud_candidate_bridge_error code=canonical_workspace_forbidden')
        return 2
    args.workspace.mkdir(parents=True, exist_ok=True)
    try:
        warm = request_json('http://127.0.0.1:11434', '/api/generate', {
            'model': MODEL, 'prompt': '', 'stream': False, 'keep_alive': '10m',
            'options': {'num_ctx': 4096, 'num_gpu': 0, 'num_thread': 4}}, timeout=90)
        if warm.get('done') is not True:
            raise ValueError('warmup_incomplete')
    except Exception:
        print('cloud_candidate_bridge_error code=model_warmup_failed')
        return 2
    failed = 0
    with tempfile.TemporaryDirectory(prefix='tl-synthetic-bridge-', dir=args.workspace) as temporary:
        for index, case in enumerate(selected_cases()):
            try:
                reasons = check_case(case, Path(temporary) / str(index))
            except Exception:
                reasons = ['integration_failed']
            failed += bool(reasons)
            print(f"cloud_candidate_bridge_case id={case['id']} result={'FAIL' if reasons else 'PASS'} codes={','.join(reasons) or 'none'}", flush=True)
    print(f'cloud_candidate_bridge_result cases=5 passed={5-failed} failed={failed} drive_access=0 canonical_write=0')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
