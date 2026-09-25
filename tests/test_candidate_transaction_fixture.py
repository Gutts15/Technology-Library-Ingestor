#!/usr/bin/env python3
"""End-to-end fixture tests for candidate transaction commit and rollback semantics."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_transaction_fixture import (
    FIXTURE_MARKER,
    FIXTURE_MARKER_VALUE,
    simulate_transaction,
)
from candidate_transaction_plan import build_transaction_plan
from library_index_build import load_records, planned_indexes, write_local_atomic


BASE_TARGET = "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-existing-bridge.md"
NEW_TARGET = "00_LIBRARY/07_DESIGN_CREATIVE/TOOLS/technology-new-bridge.md"
NEW_DOMAIN_INDEX = "00_LIBRARY/07_DESIGN_CREATIVE/INDEX.md"
NEW_CONTENT_PATH = "99_INBOX/CANDIDATES/PUBLISH_READY/records/new-record.md"
UPDATE_CONTENT_PATH = "99_INBOX/CANDIDATES/UPDATE_READY/records/aaaaaaaaaaaaaaaaaaaa.md"


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def base_record() -> bytes:
    return """RECORD_ID: aaaaaaaaaaaaaaaaaaaa
TYPE: TECHNOLOGY
STATUS: TEST
DOMAIN: 04_AI_AGENTS
CATEGORY: INTEGRATIONS
TITLE: Existing Bridge

# Existing Bridge

## SUMMARY

Stable existing summary.
""".encode("utf-8")


def updated_record() -> bytes:
    return """RECORD_ID: aaaaaaaaaaaaaaaaaaaa
TYPE: TECHNOLOGY
STATUS: REFERENCE
DOMAIN: 04_AI_AGENTS
CATEGORY: INTEGRATIONS
TITLE: Existing Bridge

# Existing Bridge

## SUMMARY

Stable existing summary.

## VERIFIED CAPABILITIES

- Existing Bridge can export verified project data.
""".encode("utf-8")


def new_record() -> bytes:
    return """RECORD_ID: bbbbbbbbbbbbbbbbbbbb
TYPE: TECHNOLOGY
STATUS: TEST
DOMAIN: 07_DESIGN_CREATIVE
CATEGORY: TOOLS
TITLE: New Bridge

# New Bridge

## SUMMARY

New verified bridge.
""".encode("utf-8")


def prepare_fixture(root: Path) -> tuple[bytes, dict, dict, dict]:
    (root / FIXTURE_MARKER).write_text(FIXTURE_MARKER_VALUE, encoding="utf-8")
    base = base_record()
    update = updated_record()
    new = new_record()

    (root / BASE_TARGET).parent.mkdir(parents=True, exist_ok=True)
    (root / BASE_TARGET).write_bytes(base)
    (root / NEW_CONTENT_PATH).parent.mkdir(parents=True, exist_ok=True)
    (root / NEW_CONTENT_PATH).write_bytes(new)
    (root / UPDATE_CONTENT_PATH).parent.mkdir(parents=True, exist_ok=True)
    (root / UPDATE_CONTENT_PATH).write_bytes(update)

    records = load_records(root, None)
    for path, content in planned_indexes(records).items():
        write_local_atomic(root / path, content)
    master = (root / "00_LIBRARY/MASTER_INDEX.md").read_bytes()

    new_manifest = {
        "schema_version": 1,
        "publish_version": "0.2.0",
        "items": [
            {
                "package_id": "c" * 20,
                "revision_key": "d" * 20,
                "record_id": "b" * 20,
                "record_type": "TECHNOLOGY",
                "status": "TEST",
                "domain": "07_DESIGN_CREATIVE",
                "category": "TOOLS",
                "slug": "new-bridge",
                "title": "New Bridge",
                "content_path": NEW_CONTENT_PATH,
                "content_sha256": sha(new),
            }
        ],
    }
    new_validation = {
        "schema_version": 1,
        "validate_version": "0.1.0",
        "canonical_write_performed": False,
        "items": [
            {
                "record_id": "b" * 20,
                "record_type": "TECHNOLOGY",
                "status": "TEST",
                "title": "New Bridge",
                "target_path": NEW_TARGET,
                "content_sha256": sha(new),
                "action": "CREATE",
            }
        ],
        "counts": {"create": 1, "unchanged": 0},
    }
    update_artifact = {
        "schema_version": 1,
        "update_ready_version": "0.2.0",
        "merge_render_version": "0.1.0",
        "candidate_id": "e" * 20,
        "record_id": "a" * 20,
        "target_path": BASE_TARGET,
        "base_sha256": sha(base),
        "merged_sha256": sha(update),
        "content_path": UPDATE_CONTENT_PATH,
        "requires_base_sha_match_before_write": True,
        "canonical_write_performed": False,
    }
    plan, errors = build_transaction_plan(master, new_manifest, new_validation, [update_artifact])
    assert not errors, errors
    assert plan is not None
    assert plan["transaction_plan_version"] == "0.2.0"
    assert plan["counts"]["create"] == 1
    assert plan["counts"]["update"] == 1
    return master, plan, new_manifest, update_artifact


def main() -> None:
    # Success path: CREATE + UPDATE + cross-domain index creation + exact verification.
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        old_master, plan, _, _ = prepare_fixture(root)
        assert not (root / NEW_DOMAIN_INDEX).exists()
        report, errors = simulate_transaction(root, plan)
        assert not errors, errors
        assert report is not None
        assert report["state"] == "COMMITTED_FIXTURE"
        assert report["writes"] == 2
        assert report["rollback_performed"] is False
        assert report["canonical_write_performed"] is False
        assert (root / BASE_TARGET).read_bytes() == updated_record()
        assert (root / NEW_TARGET).read_bytes() == new_record()
        assert (root / NEW_DOMAIN_INDEX).exists()
        new_master = (root / "00_LIBRARY/MASTER_INDEX.md").read_bytes()
        assert new_master != old_master
        assert b"RECORDS: 2" in new_master
        assert b"Existing Bridge" in new_master and b"New Bridge" in new_master

    # Partial record failure must restore records and indexes byte-for-byte.
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        old_master, plan, _, _ = prepare_fixture(root)
        old_base = (root / BASE_TARGET).read_bytes()
        report, errors = simulate_transaction(root, plan, fail_after_writes=1)
        assert errors and "injected_failure_after_write" in errors
        assert report is not None
        assert report["state"] == "ROLLED_BACK"
        assert report["rollback_verified"] is True
        assert (root / BASE_TARGET).read_bytes() == old_base
        assert not (root / NEW_TARGET).exists()
        assert not (root / NEW_DOMAIN_INDEX).exists()
        assert (root / "00_LIBRARY/MASTER_INDEX.md").read_bytes() == old_master

    # Failure after index rebuild must remove an index that did not exist before.
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        old_master, plan, _, _ = prepare_fixture(root)
        old_base = (root / BASE_TARGET).read_bytes()
        assert not (root / NEW_DOMAIN_INDEX).exists()
        report, errors = simulate_transaction(root, plan, fail_after_index_rebuild=True)
        assert errors and "injected_failure_after_index_rebuild" in errors
        assert report is not None
        assert report["state"] == "ROLLED_BACK"
        assert report["rollback_verified"] is True
        assert (root / BASE_TARGET).read_bytes() == old_base
        assert not (root / NEW_TARGET).exists()
        assert not (root / NEW_DOMAIN_INDEX).exists()
        assert (root / "00_LIBRARY/MASTER_INDEX.md").read_bytes() == old_master

    # No explicit marker, no simulation. Humans do not get accidental foot-guns for free.
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        report, errors = simulate_transaction(root, {})
        assert report is None
        assert errors == ["fixture_marker_missing"]

    print(
        "candidate_transaction_fixture_smoke_ok create=1 update=1 new_domain_index=1 "
        "rollback_after_write=1 rollback_after_index=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
