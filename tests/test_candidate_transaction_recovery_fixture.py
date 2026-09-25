#!/usr/bin/env python3
"""Smoke tests for durable fixture recovery after simulated hard interruption."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_transaction_fixture import FIXTURE_MARKER, FIXTURE_MARKER_VALUE, simulate_transaction
from candidate_transaction_plan import build_transaction_plan
from candidate_transaction_recovery_fixture import (
    mark_journal_committed,
    prepare_recovery_journal,
    recover_from_journal,
)
from library_index_build import load_records, planned_indexes, write_local_atomic


BASE_TARGET = "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-existing-bridge.md"
NEW_TARGET = "00_LIBRARY/09_NEW_DOMAIN/TOOLS/technology-new-tool.md"
NEW_CONTENT_PATH = "99_INBOX/CANDIDATES/PUBLISH_READY/records/new-tool.md"
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

Original state.
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

Original state.

## VERIFIED CAPABILITIES

- Durable update.
""".encode("utf-8")


def new_record() -> bytes:
    return """RECORD_ID: bbbbbbbbbbbbbbbbbbbb
TYPE: TECHNOLOGY
STATUS: TEST
DOMAIN: 09_NEW_DOMAIN
CATEGORY: TOOLS
TITLE: New Tool

# New Tool

## SUMMARY

Creates a previously absent domain index.
""".encode("utf-8")


def prepare_fixture(root: Path) -> tuple[dict, bytes]:
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

    manifest = {
        "schema_version": 1,
        "publish_version": "0.2.0",
        "items": [
            {
                "package_id": "c" * 20,
                "revision_key": "d" * 20,
                "record_id": "b" * 20,
                "record_type": "TECHNOLOGY",
                "status": "TEST",
                "domain": "09_NEW_DOMAIN",
                "category": "TOOLS",
                "slug": "new-tool",
                "title": "New Tool",
                "content_path": NEW_CONTENT_PATH,
                "content_sha256": sha(new),
            }
        ],
    }
    validation = {
        "schema_version": 1,
        "validate_version": "0.1.0",
        "canonical_write_performed": False,
        "items": [
            {
                "record_id": "b" * 20,
                "record_type": "TECHNOLOGY",
                "status": "TEST",
                "title": "New Tool",
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
        "base_sha256": sha(base),
        "merged_sha256": sha(update),
        "proposed_status": "REFERENCE",
        "claims_input": 1,
        "claims_added": 1,
        "claims_deduped": 0,
        "target_path": BASE_TARGET,
        "content_path": UPDATE_CONTENT_PATH,
        "requires_base_sha_match_before_write": True,
        "canonical_write_performed": False,
    }
    plan, errors = build_transaction_plan(master, manifest, validation, [update_artifact])
    assert not errors, errors
    assert plan is not None
    return plan, master


def main() -> None:
    # Hard-interruption path: reconstruct the exact old state using only the durable journal.
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        plan, master_before = prepare_fixture(root)
        base_before = (root / BASE_TARGET).read_bytes()
        existing_domain_index = root / "00_LIBRARY/04_AI_AGENTS/INDEX.md"
        existing_domain_index_before = existing_domain_index.read_bytes()
        new_domain_index = root / "00_LIBRARY/09_NEW_DOMAIN/INDEX.md"
        assert not new_domain_index.exists()

        journal, errors = prepare_recovery_journal(root, plan)
        assert not errors, errors
        assert journal is not None
        assert journal["state"] == "PREPARED"
        assert journal["canonical_write_performed"] is False
        assert journal["rollback_verified"] is False

        # Simulate a process that disappears after mutating records and rebuilding
        # indexes. No in-process exception handler is available to help us now.
        write_local_atomic(root / BASE_TARGET, updated_record())
        write_local_atomic(root / NEW_TARGET, new_record())
        records = load_records(root, None)
        for path, content in planned_indexes(records).items():
            write_local_atomic(root / path, content)
        assert new_domain_index.exists()
        assert (root / "00_LIBRARY/MASTER_INDEX.md").read_bytes() != master_before

        recovered, recovery_errors = recover_from_journal(root, plan["transaction_id"])
        assert not recovery_errors, recovery_errors
        assert recovered is not None
        assert recovered["state"] == "RECOVERED"
        assert recovered["rollback_verified"] is True
        assert (root / BASE_TARGET).read_bytes() == base_before
        assert not (root / NEW_TARGET).exists()
        assert (root / "00_LIBRARY/MASTER_INDEX.md").read_bytes() == master_before
        assert existing_domain_index.read_bytes() == existing_domain_index_before
        assert not new_domain_index.exists()

        # Recovery is idempotent. A second invocation keeps the same clean state.
        recovered_again, second_errors = recover_from_journal(root, plan["transaction_id"])
        assert not second_errors, second_errors
        assert recovered_again is not None and recovered_again["state"] == "RECOVERED"
        assert (root / BASE_TARGET).read_bytes() == base_before
        assert (root / "00_LIBRARY/MASTER_INDEX.md").read_bytes() == master_before

    # Successful path: a PREPARED journal cannot be retired on optimism. It is
    # marked COMMITTED only after the fixture's verified receipt proves final state.
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        plan, _ = prepare_fixture(root)
        journal, journal_errors = prepare_recovery_journal(root, plan)
        assert not journal_errors and journal is not None

        bad_receipt = {
            "schema_version": 1,
            "receipt_version": "0.2.0",
            "transaction_id": plan["transaction_id"],
            "state": "VERIFIED_COMMITTED_STATE",
            "master_index_before_sha256": plan["master_index_sha256"],
            "master_index_after_sha256": "f" * 64,
            "candidate_settlement_eligible": False,
            "verification_only": True,
            "canonical_write_performed": False,
        }
        blocked, blocked_errors = mark_journal_committed(root, plan["transaction_id"], bad_receipt)
        assert blocked is None
        assert "receipt_not_settlement_eligible" in blocked_errors

        report, transaction_errors = simulate_transaction(root, plan)
        assert not transaction_errors, transaction_errors
        assert report is not None and report["state"] == "COMMITTED_FIXTURE"
        receipt = report["receipt"]
        committed, commit_errors = mark_journal_committed(root, plan["transaction_id"], receipt)
        assert not commit_errors, commit_errors
        assert committed is not None
        assert committed["state"] == "COMMITTED"
        assert committed["receipt_verified"] is True
        assert committed["master_index_after_sha256"] == receipt["master_index_after_sha256"]

        # A committed journal is not a rollback request anymore.
        no_recovery, no_recovery_errors = recover_from_journal(root, plan["transaction_id"])
        assert no_recovery is None
        assert no_recovery_errors == ["recovery_state"]

        # Commit marking is idempotent once the receipt was verified.
        committed_again, again_errors = mark_journal_committed(root, plan["transaction_id"], receipt)
        assert not again_errors
        assert committed_again == committed

    # Journal creation also requires the explicit fixture marker.
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        journal, errors = prepare_recovery_journal(root, {})
        assert journal is None
        assert errors == ["fixture_marker_missing"]

    print(
        "candidate_transaction_recovery_fixture_smoke_ok durable_journal=1 hard_interrupt=1 "
        "new_index_cleanup=1 idempotent_recovery=1 receipt_retirement=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
