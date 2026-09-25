#!/usr/bin/env python3
"""Smoke tests for the transport-agnostic canonical record mutation engine."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_canonical_record_mutation import execute_record_mutations


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def plan(base: bytes, update: bytes, create: bytes, unchanged: bytes) -> dict:
    return {
        "schema_version": 1,
        "transaction_plan_version": "0.2.0",
        "transaction_id": "a" * 20,
        "master_index_sha256": "b" * 64,
        "canonical_write_performed": False,
        "requires_live_revalidation": True,
        "requires_atomic_record_writes": True,
        "requires_index_rebuild_after_write": True,
        "requires_byte_verification_after_write": True,
        "requires_rollback_on_partial_failure": True,
        "items": [
            {
                "lane": "UPDATE",
                "action": "UPDATE",
                "candidate_id": "c" * 20,
                "record_id": "d" * 20,
                "target_path": "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-existing.md",
                "content_path": "99_INBOX/CANDIDATES/UPDATE_READY/records/update.md",
                "content_sha256": sha(update),
                "base_sha256": sha(base),
                "precondition": "EXACT_BASE_SHA",
            },
            {
                "lane": "NEW",
                "action": "CREATE",
                "package_id": "e" * 20,
                "revision_key": "f" * 20,
                "record_id": "1" * 20,
                "target_path": "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-created.md",
                "content_path": "99_INBOX/CANDIDATES/PUBLISH_READY/records/create.md",
                "content_sha256": sha(create),
                "precondition": "TARGET_ABSENT",
            },
            {
                "lane": "NEW",
                "action": "UNCHANGED",
                "package_id": "2" * 20,
                "revision_key": "3" * 20,
                "record_id": "4" * 20,
                "target_path": "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-unchanged.md",
                "content_path": "99_INBOX/CANDIDATES/PUBLISH_READY/records/unchanged.md",
                "content_sha256": sha(unchanged),
                "precondition": "EXACT_BYTES_PRESENT",
            },
        ],
        "counts": {"create": 1, "update": 1, "unchanged": 1, "writes": 2},
    }


def main() -> None:
    base = b"base\n"
    update = b"updated\n"
    create = b"created\n"
    unchanged = b"unchanged\n"

    store = {
        "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-existing.md": base,
        "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-unchanged.md": unchanged,
        "99_INBOX/CANDIDATES/UPDATE_READY/records/update.md": update,
        "99_INBOX/CANDIDATES/PUBLISH_READY/records/create.md": create,
        "99_INBOX/CANDIDATES/PUBLISH_READY/records/unchanged.md": unchanged,
    }

    def read(path: str):
        return store.get(path)

    def write(path: str, raw: bytes) -> bool:
        store[path] = raw
        return True

    report, errors = execute_record_mutations(
        plan(base, update, create, unchanged),
        read_bytes=read,
        write_bytes=write,
    )
    assert not errors, errors
    assert report is not None and report["state"] == "RECORDS_VERIFIED"
    assert report["writes_completed"] == 2
    assert report["verified_items"] == 3
    assert store["00_LIBRARY/04_AI_AGENTS/TOOLS/technology-existing.md"] == update
    assert store["00_LIBRARY/04_AI_AGENTS/TOOLS/technology-created.md"] == create

    # A partial write failure must explicitly require durable recovery.
    failing_store = {
        "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-existing.md": base,
        "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-unchanged.md": unchanged,
        "99_INBOX/CANDIDATES/UPDATE_READY/records/update.md": update,
        "99_INBOX/CANDIDATES/PUBLISH_READY/records/create.md": create,
        "99_INBOX/CANDIDATES/PUBLISH_READY/records/unchanged.md": unchanged,
    }
    write_calls = 0

    def failing_read(path: str):
        return failing_store.get(path)

    def failing_write(path: str, raw: bytes) -> bool:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 2:
            return False
        failing_store[path] = raw
        return True

    report, errors = execute_record_mutations(
        plan(base, update, create, unchanged),
        read_bytes=failing_read,
        write_bytes=failing_write,
    )
    assert errors == ["item_1_write_failed"], errors
    assert report is not None and report["state"] == "RECOVERY_REQUIRED"
    assert report["writes_completed"] == 1
    assert report["canonical_mutation_started"] is True
    assert failing_store["00_LIBRARY/04_AI_AGENTS/TOOLS/technology-existing.md"] == update
    assert "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-created.md" not in failing_store

    # Drift before the first write must abort without any mutation.
    drift_store = dict(store)
    drift_store["00_LIBRARY/04_AI_AGENTS/TOOLS/technology-existing.md"] = b"drifted\n"

    report, errors = execute_record_mutations(
        plan(base, update, create, unchanged),
        read_bytes=lambda path: drift_store.get(path),
        write_bytes=lambda path, raw: True,
    )
    assert report is None
    assert "item_0_update_base_sha_changed" in errors

    print(
        "candidate_canonical_record_mutation_smoke_ok create=1 update=1 unchanged=1 "
        "byte_verify=1 partial_failure_requires_recovery=1 prewrite_drift_blocked=1"
    )


if __name__ == "__main__":
    main()
