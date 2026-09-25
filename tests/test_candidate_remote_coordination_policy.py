#!/usr/bin/env python3
"""Smoke tests for production remote-coordination capability gating."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_remote_coordination_policy import builtin_profile, evaluate_descriptor


def safe_descriptor() -> dict:
    return {
        "schema_version": 1,
        "descriptor_version": "0.1.0",
        "coordinator_id": "fixture-cas",
        "coordination_kind": "COMPARE_AND_SWAP",
        "evidence": ["fixture:test-contract"],
        "guarantees": {
            "atomic_create_if_absent": False,
            "compare_and_swap": True,
            "read_after_write_consistency": True,
            "durable_owner_identity": True,
            "transaction_scoped_ownership": True,
            "conflict_detection": True,
            "no_silent_overwrite": True,
            "stale_owner_requires_verified_recovery": True,
            "release_requires_owner_or_verified_recovery": True,
        },
    }


def main() -> None:
    blocked_profile = builtin_profile("rclone-google-drive-path-lock")
    assert blocked_profile is not None
    blocked, blocked_errors = evaluate_descriptor(blocked_profile)
    assert not blocked_errors, blocked_errors
    assert blocked is not None
    assert blocked["state"] == "BLOCKED"
    assert blocked["exclusive_acquire_proven"] is False
    assert blocked["production_publish_authorized"] is False
    assert "exclusive_acquire_not_proven" in blocked["reasons"]
    assert "guarantee_missing:conflict_detection" in blocked["reasons"]
    assert "guarantee_missing:no_silent_overwrite" in blocked["reasons"]

    safe, safe_errors = evaluate_descriptor(safe_descriptor())
    assert not safe_errors, safe_errors
    assert safe is not None
    assert safe["state"] == "READY_FOR_EXECUTOR_DESIGN"
    assert safe["exclusive_acquire_proven"] is True
    assert safe["reasons"] == []
    # Passing the coordination capability gate is deliberately not publication
    # authorization. The real-batch/editorial/executor gates still remain.
    assert safe["production_publish_authorized"] is False
    assert safe["canonical_write_performed"] is False

    weak = safe_descriptor()
    weak["guarantees"]["read_after_write_consistency"] = False
    weak_report, weak_errors = evaluate_descriptor(weak)
    assert not weak_errors, weak_errors
    assert weak_report is not None and weak_report["state"] == "BLOCKED"
    assert "guarantee_missing:read_after_write_consistency" in weak_report["reasons"]

    malformed = safe_descriptor()
    malformed["guarantees"]["compare_and_swap"] = "yes"
    malformed_report, malformed_errors = evaluate_descriptor(malformed)
    assert malformed_report is None
    assert malformed_errors == ["guarantee_values_must_be_boolean"]

    print(
        "candidate_remote_coordination_policy_smoke_ok rclone_drive_blocked=1 cas_ready=1 "
        "consistency_required=1 authorization_separate=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
