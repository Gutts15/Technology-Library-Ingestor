"""Local private-mirror E1 integration, never a production/Drive publisher.

Content-addressed extraction receipts rebuild the existing FILE_EVIDENCE index
before preparing the next immutable lot. Requires a deliberate local marker,
one local writer and no Git checkout. No sources are moved or deleted. Remote
intake remains disabled until shared coordinator qualification is authorized.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import time
from datetime import date
from pathlib import Path

from e1_lote_prepare import FILE_ROOT, Storage, encode, prepare, validate_index
from e1_source_extract import EXTRACTOR_VERSION, MAX_INPUT_BYTES, ExtractionError, extract_file
from e3_recovery_fixture import sync_write
from ready_evidence_bridge import build_index

MARKER = '99_INBOX/CURATION/_e1-local-source.marker'
MARKER_BYTES = b'E1_PRIVATE_LOCAL_ONLY\n'
RECEIPTS = '99_INBOX/CURATION/E1_SOURCE_RECEIPTS'
MAX_SOURCES = 20


class SourcePreparationError(Exception):
    pass


def target(root: Path, relative: str) -> Path:
    path = root / relative
    if path.resolve() != path or not path.is_relative_to(root):
        raise SourcePreparationError('unsafe_local_path')
    return path


def persist(root: Path, relative: str, raw: bytes, *, replace=False) -> None:
    path = target(root, relative)
    if path.exists() and not replace:
        if path.read_bytes() != raw:
            raise SourcePreparationError('immutable_local_conflict')
        return
    # Reuse the existing fsync + atomic replacement primitive. This does not
    # substitute a local lock for the blocked shared production coordinator.
    sync_write(path, raw)


def rebuild(root: Path) -> tuple[list[dict], dict[str, dict]]:
    receipts = sorted(target(root, RECEIPTS).glob('*.json'))
    if len(receipts) > 200:
        raise SourcePreparationError('local_source_inventory_limit')
    envelopes, known, superseded = [], {}, set()
    for path in receipts:
        if path.is_symlink() or path.stat().st_size > 4096:
            raise SourcePreparationError('local_receipt_invalid')
        row = json.loads(path.read_bytes())
        sha = row.get('source_sha256')
        if (not isinstance(sha, str) or len(sha) != 64
                or any(c not in '0123456789abcdef' for c in sha)
                or path.name != sha + '.json' or sha in known):
            raise SourcePreparationError('local_receipt_invalid')
        pid = row.get('package_id')
        if not isinstance(pid, str) or len(pid) != 20 or any(c not in '0123456789abcdef' for c in pid):
            raise SourcePreparationError('local_receipt_invalid')
        raw = target(root, FILE_ROOT + '/' + pid + '.json').read_bytes()
        env = json.loads(raw)
        evidence = target(root, '99_INBOX/READY_FOR_ANALYSIS/' + pid + '/evidence-summary.json').read_bytes()
        if (hashlib.sha256(raw).hexdigest() != row.get('envelope_sha256')
                or hashlib.sha256(evidence).hexdigest() != env.get('evidence_summary_sha256')
                or env.get('package_id') != pid or row.get('state') != env.get('evidence_assessment', {}).get('state')):
            raise SourcePreparationError('local_receipt_binding_invalid')
        envelopes.append(env)
        known[sha] = row
        for old_pid in row.get('supersedes', []):
            if isinstance(old_pid, str):
                superseded.add(old_pid)
    index = build_index(envelopes, [])
    validate_index(index)
    existing = target(root, FILE_ROOT + '/index.json')
    if existing.exists():
        previous_index = json.loads(existing.read_bytes())
        previous = validate_index(previous_index)
        if previous_index.get('held', 0) != 0 or previous_index.get('held_items', []) != []:
            raise SourcePreparationError('foreign_evidence_index')
        owned = {row['package_id']: row for row in index['items']}
        if any(row != {key: owned.get(row['package_id'], {}).get(key) for key in row}
               for row in previous if row['package_id'] not in superseded):
            # This marked mirror is not permission to discard older/native
            # evidence. Foreign entries need explicit integration, not reset.
            raise SourcePreparationError('foreign_evidence_index')
    persist(root, FILE_ROOT + '/index.json', encode(index), replace=True)
    return envelopes, known


def source_checksum(source: Path) -> str:
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INPUT_BYTES:
            raise SourcePreparationError('source_invalid')
        raw = stream.read(MAX_INPUT_BYTES + 1)
        after = os.fstat(stream.fileno())
        if (len(raw) != info.st_size or len(raw) > MAX_INPUT_BYTES
                or info.st_mtime_ns != after.st_mtime_ns or info.st_size != after.st_size):
            raise SourcePreparationError('source_changed_during_read')
    return hashlib.sha256(raw).hexdigest()


def _prepare_sources(root: Path, sources: list[Path], day: str, *, budget_seconds=300,
                     local_whisper_model: Path | None = None) -> dict[str, int]:
    root = Path(os.path.abspath(root))
    if root.resolve() != root or any((p / '.git').exists() for p in [root, *root.parents]):
        raise SourcePreparationError('private_local_mirror_required')
    marker = target(root, MARKER)
    if (not marker.is_file() or marker.read_bytes() != MARKER_BYTES
            or len(sources) > MAX_SOURCES or not 0 < budget_seconds <= 900):
        raise SourcePreparationError('local_preparation_not_authorized')
    if date.fromisoformat(day).isoformat() != day:
        raise SourcePreparationError('date_invalid')
    lock = target(root, '99_INBOX/CURATION/.e1-local-source.lock')
    with lock.open('a+b') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        deadline = time.monotonic() + budget_seconds
        _, known = rebuild(root)
        stats = {'extracted': 0, 'duplicates': 0, 'held': 0, 'lotes_written': 0}
        for source in sources:
            source = Path(os.path.abspath(source))
            if (source.resolve() != source or not source.is_file()
                    or source.stat().st_size > MAX_INPUT_BYTES):
                raise SourcePreparationError('source_invalid')
            sha = source_checksum(source)
            if sha in known and known[sha].get('extractor_version', 1) >= EXTRACTOR_VERSION:
                stats['duplicates'] += 1
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SourcePreparationError('source_cycle_budget_exceeded')
            with tempfile.TemporaryDirectory(prefix='tl-e1-decode-') as temp:
                extracted = extract_file(source, Path(temp) / 'artifacts',
                    budget_seconds=remaining, local_whisper_model=local_whisper_model)
                if extracted.source_sha256 != sha:
                    raise SourcePreparationError('source_changed_during_extraction')
                pid = extracted.package_id
                persist(root, '99_INBOX/READY_FOR_ANALYSIS/' + pid + '/evidence-summary.json',
                        extracted.evidence_raw)
                envelope_raw = encode(extracted.envelope)
                persist(root, FILE_ROOT + '/' + pid + '.json', envelope_raw)
                receipt = {'schema_version': 1, 'source_sha256': sha, 'package_id': pid,
                           'state': extracted.assessment['state'],
                           'envelope_sha256': hashlib.sha256(envelope_raw).hexdigest(),
                           'extractor_version': EXTRACTOR_VERSION}
                previous = known.get(sha)
                if previous is not None:
                    # Re-read by a newer extractor: the old package leaves the
                    # index (its files stay on Drive, nothing is deleted).
                    chain = list(previous.get('supersedes', []))
                    if previous['package_id'] != pid:
                        chain.append(previous['package_id'])
                    receipt['supersedes'] = chain
                persist(root, RECEIPTS + '/' + sha + '.json', encode(receipt),
                        replace=previous is not None)
                stats['extracted'] += 1
                stats['held'] += int(receipt['state'] == 'HELD')
                known[sha] = receipt
        rebuild(root)
        lot, path, _ = prepare(Storage(root), day)
        if lot is not None:
            persist(root, path, encode(lot))
            stats['lotes_written'] = 1
        return stats


def prepare_sources(root: Path, sources: list[Path], day: str, *, budget_seconds=300,
                    local_whisper_model: Path | None = None) -> dict[str, int]:
    try:
        return _prepare_sources(root, sources, day, budget_seconds=budget_seconds,
                                local_whisper_model=local_whisper_model)
    except (OSError, ValueError, TypeError, KeyError):
        # Filesystem/JSON exception strings may contain private paths/content.
        raise SourcePreparationError('local_source_state_unavailable_or_invalid') from None
