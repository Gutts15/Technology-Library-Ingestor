#!/usr/bin/env python3
"""Deterministic smoke tests for the guarded remote executor wiring fixture."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_executor_remote_wiring_fixture import (
    CREATED_BYTES,
    AFTER_BYTES,
    BEFORE_BYTES,
    FIXTURE_ROOT,
    build_prepared_journal,
    build_receipt,
    executor_fixture_ref,
    fixture_prefix,
    safe_fixture_id,
    validate_journal,
    validate_receipt,
)


def main() -> None:
    fixture_id = "1234567890abcdef12345678"
    transaction_id = "a" * 20
    owner = "executor-fixture-test"
    ref = executor_fixture_ref(fixture_id)

    assert safe_fixture_id(fixture_id)
    assert not safe_fixture_id("bad")
    assert fixture_prefix(fixture_id) == f"{FIXTURE_ROOT}/{fixture_id}"
    assert fixture_prefix(fixture_id).startswith("99_INBOX/CANDIDATES/")
    assert "00_LIBRARY" not in fixture_prefix(fixture_id)
    assert ref.startswith("refs/heads/tl-coordination-fixture-executor-")

    journal = build_prepared_journal(fixture_id, transaction_id, owner, ref)
    assert journal["state"] == "PREPARED"
    assert journal["fixture_only"] is True
    assert journal["live_preconditions_verified"] is True
    assert journal["fixture_write_ready"] is False
    assert journal["production_publish_authorized"] is False
    assert journal["canonical_write_performed"] is False
    assert validate_journal(
        journal,
        fixture_id,
        transaction_id,
        owner,
        ref,
        expected_state="PREPARED",
    ) == []

    escaped = dict(journal)
    escaped["existing_target_path"] = "00_LIBRARY/not-allowed.md"
    assert "journal_scope:existing_target_path" in validate_journal(
        escaped,
        fixture_id,
        transaction_id,
        owner,
        ref,
        expected_state="PREPARED",
    )

    dirty = dict(journal)
    dirty["canonical_write_performed"] = True
    assert "journal_canonical_write_flag" in validate_journal(
        dirty,
        fixture_id,
        transaction_id,
        owner,
        ref,
        expected_state="PREPARED",
    )

    receipt = build_receipt(fixture_id, transaction_id, owner)
    assert receipt["state"] == "VERIFIED_COMMITTED_FIXTURE"
    assert receipt["candidate_settlement_eligible"] is False
    assert receipt["production_publish_authorized"] is False
    assert receipt["canonical_write_performed"] is False
    assert validate_receipt(receipt, fixture_id, transaction_id, owner) == []
    assert len(receipt["targets"]) == 2
    assert BEFORE_BYTES != AFTER_BYTES != CREATED_BYTES

    bad_receipt = dict(receipt)
    bad_receipt["candidate_settlement_eligible"] = True
    assert "receipt_settlement_flag" in validate_receipt(
        bad_receipt,
        fixture_id,
        transaction_id,
        owner,
    )

    print(
        "candidate_executor_remote_wiring_fixture_smoke_ok private_scope=1 "
        "coordinator_contract=1 prewrite_journal=1 receipt_contract=1 "
        "settlement_blocked=1 authorization_separate=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
