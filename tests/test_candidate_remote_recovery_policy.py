#!/usr/bin/env python3
"""Smoke tests for remote recovery capability policy gating."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_remote_recovery_policy import evaluate_descriptor


def safe_descriptor() -> dict:
    return {
        "schema_version": 1,
        "descriptor_version": "0.1.0",
        "storage_id": "fixture-recovery",
        "recovery_kind": "DURABLE_REMOTE_SNAPSHOT_JOURNAL",
        "evidence": ["fixture:test-contract"],
        "guarantees": {
            "durable_journal_persistence": True,
            "snapshot_integrity_verified": True,
            "restart_independent_recovery": True,
            "byte_exact_rollback_verified": True,
            "terminal_recovery_state_verified": True,
            "private_recovery_scope": True,
            "cleanup_verified": True,
        },
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }


def main() -> None:
    safe, safe_errors = evaluate_descriptor(safe_descriptor())
    assert not safe_errors, safe_errors
    assert safe is not None
    assert safe["state"] == "READY_FOR_EXECUTOR_DESIGN"
    assert safe["recovery_capability_proven"] is True
    assert safe["reasons"] == []
    assert safe["production_publish_authorized"] is False
    assert safe["canonical_write_performed"] is False

    weak = safe_descriptor()
    weak["guarantees"]["byte_exact_rollback_verified"] = False
    weak_report, weak_errors = evaluate_descriptor(weak)
    assert not weak_errors, weak_errors
    assert weak_report is not None
    assert weak_report["state"] == "BLOCKED"
    assert weak_report["recovery_capability_proven"] is False
    assert "guarantee_missing:byte_exact_rollback_verified" in weak_report["reasons"]

    malformed = safe_descriptor()
    malformed["guarantees"]["cleanup_verified"] = "yes"
    malformed_report, malformed_errors = evaluate_descriptor(malformed)
    assert malformed_report is None
    assert malformed_errors == ["guarantee_values_must_be_boolean"]

    dirty = safe_descriptor()
    dirty["canonical_write_performed"] = True
    dirty_report, dirty_errors = evaluate_descriptor(dirty)
    assert dirty_report is None
    assert "descriptor_write_flag" in dirty_errors

    print(
        "candidate_remote_recovery_policy_smoke_ok recovery_ready=1 rollback_required=1 "
        "private_scope_required=1 authorization_separate=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
