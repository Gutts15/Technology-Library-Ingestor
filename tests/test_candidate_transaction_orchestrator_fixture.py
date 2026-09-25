#!/usr/bin/env python3
"""Smoke tests for full lease/journal/transaction/receipt orchestration in fixtures."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_transaction_fixture import FIXTURE_MARKER, FIXTURE_MARKER_VALUE
from candidate_transaction_lease_fixture import lease_path
from candidate_transaction_orchestrator_fixture import run_fixture_transaction
from candidate_transaction_plan import build_transaction_plan
from candidate_transaction_recovery_fixture import journal_dir
from library_index_build import load_records, planned_indexes, write_local_atomic


EXISTING_TARGET = "00_LIBRARY/01_GAME_DEVELOPMENT/CODE/pattern-existing-pattern.md"
NEW_TARGET = "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-new-bridge.md"
NEW_CONTENT_PATH = "99_INBOX/CANDIDATES/PUBLISH_READY/records/bbbbbbbbbbbbbbbbbbbb.md"


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def existing_record() -> bytes:
    return """RECORD_ID: aaaaaaaaaaaaaaaaaaaa
TYPE: PATTERN
STATUS: REFERENCE
DOMAIN: 01_GAME_DEVELOPMENT
CATEGORY: CODE
TITLE: Existing Pattern

# Existing Pattern

## PURPOSE

Existing canonical knowledge.
""".encode("utf-8")


def new_record() -> bytes:
    return """RECORD_ID: bbbbbbbbbbbbbbbbbbbb
TYPE: TECHNOLOGY
STATUS: TEST
DOMAIN: 04_AI_AGENTS
CATEGORY: INTEGRATIONS
TITLE: New Bridge

# New Bridge

## SUMMARY

Controls an editor through a bridge.
""".encode("utf-8")


def prepare_fixture(root: Path) -> tuple[dict, bytes]:
    (root / FIXTURE_MARKER).write_text(FIXTURE_MARKER_VALUE, encoding="utf-8")
    existing = existing_record()
    new = new_record()
    (root / EXISTING_TARGET).parent.mkdir(parents=True, exist_ok=True)
    (root / EXISTING_TARGET).write_bytes(existing)
    (root / NEW_CONTENT_PATH).parent.mkdir(parents=True, exist_ok=True)
    (root / NEW_CONTENT_PATH).write_bytes(new)

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
                "domain": "04_AI_AGENTS",
                "category": "INTEGRATIONS",
                "slug": "new-bridge",
                "title": "New Bridge",
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
                "title": "New Bridge",
                "target_path": NEW_TARGET,
                "content_sha256": sha(new),
                "action": "CREATE",
            }
        ],
        "counts": {"create": 1, "unchanged": 0},
    }
    plan, errors = build_transaction_plan(master, manifest, validation, [])
    assert not errors, errors
    assert plan is not None
    return plan, master


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        plan, master_before = prepare_fixture(root)
        report, errors = run_fixture_transaction(root, plan, owner="fixture-owner")
        assert not errors, errors
        assert report is not None
        assert report["state"] == "COMMITTED_FIXTURE_FULL_SEQUENCE"
        assert report["transaction_state"] == "COMMITTED_FIXTURE"
        assert report["journal_state"] == "COMMITTED"
        assert report["receipt_verified"] is True
        assert report["lease_active"] is False
        assert report["canonical_write_performed"] is False
        assert not lease_path(root).exists()
        assert (root / NEW_TARGET).read_bytes() == new_record()
        journal = (journal_dir(root, plan["transaction_id"]) / "journal.json").read_text(encoding="utf-8")
        assert '"state": "COMMITTED"' in journal
        assert '"receipt_verified": true' in journal
        assert (root / "00_LIBRARY/MASTER_INDEX.md").read_bytes() != master_before

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        plan, master_before = prepare_fixture(root)
        report, errors = run_fixture_transaction(
            root,
            plan,
            owner="fixture-owner",
            fail_after_writes=1,
        )
        assert errors
        assert any(code == "transaction:injected_failure_after_write" for code in errors)
        assert report is not None
        assert report["state"] == "RECOVERED_AFTER_FAILURE"
        assert report["journal_state"] == "RECOVERED"
        assert report["rollback_verified"] is True
        assert report["lease_active"] is False
        assert report["canonical_write_performed"] is False
        assert not lease_path(root).exists()
        assert not (root / NEW_TARGET).exists()
        assert (root / "00_LIBRARY/MASTER_INDEX.md").read_bytes() == master_before

    print(
        "candidate_transaction_orchestrator_fixture_smoke_ok full_sequence=1 committed_journal=1 "
        "verified_receipt=1 failure_recovery=1 lease_finalization=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
