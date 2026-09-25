#!/usr/bin/env python3
"""Smoke test for the composed canonical transaction engine."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_canonical_transaction_engine import execute_canonical_transaction


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def record(
    rid: str,
    *,
    domain: str,
    category: str,
    title: str,
    slug: str,
    note: str,
) -> tuple[str, bytes]:
    path = f"00_LIBRARY/{domain}/{category}/technology-{slug}.md"
    raw = (
        f"RECORD_ID: {rid}\n"
        "TYPE: TECHNOLOGY\n"
        "STATUS: REFERENCE\n"
        f"DOMAIN: {domain}\n"
        f"CATEGORY: {category}\n"
        f"TITLE: {title}\n"
        f"\n# SUMMARY\n{note}\n"
    ).encode("utf-8")
    return path, raw


def build_fixture():
    existing_path, existing_before = record(
        "a" * 20,
        domain="04_AI_AGENTS",
        category="TOOLS",
        title="Existing",
        slug="existing",
        note="Before.",
    )
    _, existing_after = record(
        "a" * 20,
        domain="04_AI_AGENTS",
        category="TOOLS",
        title="Existing",
        slug="existing",
        note="After.",
    )
    created_path, created = record(
        "b" * 20,
        domain="07_DESIGN_CREATIVE",
        category="TOOLS",
        title="Created",
        slug="created",
        note="Created.",
    )
    unchanged_path, unchanged = record(
        "c" * 20,
        domain="04_AI_AGENTS",
        category="TOOLS",
        title="Unchanged",
        slug="unchanged",
        note="Stable.",
    )

    update_content = "99_INBOX/CANDIDATES/UPDATE_READY/records/update.md"
    create_content = "99_INBOX/CANDIDATES/PUBLISH_READY/records/create.md"
    unchanged_content = "99_INBOX/CANDIDATES/PUBLISH_READY/records/unchanged.md"

    plan = {
        "schema_version": 1,
        "transaction_plan_version": "0.2.0",
        "transaction_id": "d" * 20,
        "master_index_sha256": "e" * 64,
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
                "candidate_id": "1" * 20,
                "record_id": "a" * 20,
                "target_path": existing_path,
                "content_path": update_content,
                "content_sha256": sha(existing_after),
                "base_sha256": sha(existing_before),
                "precondition": "EXACT_BASE_SHA",
            },
            {
                "lane": "NEW",
                "action": "CREATE",
                "package_id": "2" * 20,
                "revision_key": "3" * 20,
                "record_id": "b" * 20,
                "target_path": created_path,
                "content_path": create_content,
                "content_sha256": sha(created),
                "precondition": "TARGET_ABSENT",
            },
            {
                "lane": "NEW",
                "action": "UNCHANGED",
                "package_id": "4" * 20,
                "revision_key": "5" * 20,
                "record_id": "c" * 20,
                "target_path": unchanged_path,
                "content_path": unchanged_content,
                "content_sha256": sha(unchanged),
                "precondition": "EXACT_BYTES_PRESENT",
            },
        ],
        "counts": {"create": 1, "update": 1, "unchanged": 1, "writes": 2},
    }
    store = {
        existing_path: existing_before,
        unchanged_path: unchanged,
        update_content: existing_after,
        create_content: created,
        unchanged_content: unchanged,
    }
    return plan, store, [existing_path, unchanged_path], created_path


def main() -> None:
    plan, store, before_paths, created_path = build_fixture()

    def read(path: str):
        return store.get(path)

    def write(path: str, raw: bytes) -> bool:
        store[path] = raw
        return True

    report, errors = execute_canonical_transaction(
        plan,
        canonical_record_paths_before=before_paths,
        read_bytes=read,
        write_record_bytes=write,
        write_index_bytes=write,
    )
    assert not errors, errors
    assert report["state"] == "CANONICAL_STATE_VERIFIED"
    assert report["records_after"] == 3
    assert report["final_verification"] is True
    assert created_path in store
    assert b"RECORDS: 3" in store["00_LIBRARY/MASTER_INDEX.md"]
    assert "00_LIBRARY/04_AI_AGENTS/INDEX.md" in store
    assert "00_LIBRARY/07_DESIGN_CREATIVE/INDEX.md" in store

    # Index failure after records mutate must require durable recovery.
    plan2, store2, before_paths2, _ = build_fixture()

    def read2(path: str):
        return store2.get(path)

    def write_record2(path: str, raw: bytes) -> bool:
        store2[path] = raw
        return True

    index_calls = 0

    def fail_first_index(path: str, raw: bytes) -> bool:
        nonlocal index_calls
        index_calls += 1
        if index_calls == 1:
            return False
        store2[path] = raw
        return True

    report, errors = execute_canonical_transaction(
        plan2,
        canonical_record_paths_before=before_paths2,
        read_bytes=read2,
        write_record_bytes=write_record2,
        write_index_bytes=fail_first_index,
    )
    assert errors and errors[0].startswith("index_write_failed:")
    assert report["state"] == "RECOVERY_REQUIRED"
    assert report["record_report"]["state"] == "RECORDS_VERIFIED"
    assert report["final_verification"] is False

    print(
        "candidate_canonical_transaction_engine_smoke_ok records_and_indexes=1 "
        "final_verify=1 index_failure_requires_recovery=1"
    )


if __name__ == "__main__":
    main()
