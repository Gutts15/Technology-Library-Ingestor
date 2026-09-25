#!/usr/bin/env python3
"""Deterministic smoke tests for executor remote restart recovery fixture."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_executor_remote_recovery_fixture import (
    build_recovered_journal,
    build_write_ready_journal,
    safe_resume_identity,
)
from candidate_executor_remote_wiring_fixture import (
    BEFORE_BYTES,
    FIXTURE_ROOT,
    build_prepared_journal,
    executor_fixture_ref,
    fixture_prefix,
    sha256,
    validate_journal,
)


def main() -> None:
    fixture_id = "1234567890abcdef12345678"
    transaction_id = "a" * 20
    owner = "executor-recovery-" + fixture_id[:12]
    ref = executor_fixture_ref(fixture_id)

    assert fixture_prefix(fixture_id).startswith(FIXTURE_ROOT + "/")
    assert "00_LIBRARY" not in fixture_prefix(fixture_id)
    assert safe_resume_identity(fixture_id, transaction_id, owner, ref)
    assert not safe_resume_identity("bad", transaction_id, owner, ref)
    assert not safe_resume_identity(fixture_id, "not-a-transaction", owner, ref)
    assert not safe_resume_identity(fixture_id, transaction_id, "wrong-owner", ref)
    assert not safe_resume_identity(fixture_id, transaction_id, owner, ref + "-wrong")

    prepared = build_prepared_journal(fixture_id, transaction_id, owner, ref)
    ready = build_write_ready_journal(prepared)
    assert ready["state"] == "WRITE_READY_FIXTURE"
    assert ready["fixture_write_ready"] is True
    assert validate_journal(
        ready,
        fixture_id,
        transaction_id,
        owner,
        ref,
        expected_state="WRITE_READY_FIXTURE",
    ) == []

    recovered = build_recovered_journal(ready)
    assert recovered["state"] == "RECOVERED"
    assert recovered["fixture_write_ready"] is False
    assert recovered["rollback_verified"] is True
    assert recovered["restart_recovery_verified"] is True
    assert recovered["restored_existing_sha256"] == sha256(BEFORE_BYTES)
    assert recovered["removed_created_target_verified"] is True
    assert recovered["receipt_verified"] is False
    assert recovered["production_publish_authorized"] is False
    assert recovered["canonical_write_performed"] is False
    assert validate_journal(
        recovered,
        fixture_id,
        transaction_id,
        owner,
        ref,
        expected_state="RECOVERED",
    ) == []

    escaped = dict(recovered)
    escaped["existing_target_path"] = "00_LIBRARY/not-allowed.md"
    assert "journal_scope:existing_target_path" in validate_journal(
        escaped,
        fixture_id,
        transaction_id,
        owner,
        ref,
        expected_state="RECOVERED",
    )

    print(
        "candidate_executor_remote_recovery_fixture_smoke_ok private_scope=1 "
        "restart_identity=1 write_ready_contract=1 recovered_contract=1 "
        "rollback_binding=1 authorization_separate=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
