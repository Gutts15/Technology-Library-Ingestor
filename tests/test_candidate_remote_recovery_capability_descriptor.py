#!/usr/bin/env python3
"""Smoke tests for live remote-recovery capability descriptor derivation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_remote_recovery_capability_descriptor import STORAGE_ID, derive_descriptor
from candidate_remote_recovery_policy import evaluate_descriptor


def passed_probe() -> dict:
    fixture_id = "1234567890abcdef12345678"
    return {
        "schema_version": 1,
        "probe_version": "0.1.0",
        "state": "PASS",
        "fixture_id": fixture_id,
        "fixture_path": f"99_INBOX/CANDIDATES/RECOVERY_REMOTE_FIXTURE/{fixture_id}",
        "journal_persisted": True,
        "snapshot_verified": True,
        "restart_recovery_verified": True,
        "rollback_verified": True,
        "cleanup_verified": True,
        "cleanup_required": False,
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }


def encode(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main() -> None:
    descriptor, errors = derive_descriptor(encode(passed_probe()))
    assert not errors, errors
    assert descriptor is not None
    assert descriptor["storage_id"] == STORAGE_ID == "rclone-private-recovery"
    assert descriptor["recovery_kind"] == "DURABLE_REMOTE_SNAPSHOT_JOURNAL"
    for value in descriptor["guarantees"].values():
        assert value is True
    assert descriptor["production_publish_authorized"] is False
    assert descriptor["canonical_write_performed"] is False

    policy, policy_errors = evaluate_descriptor(descriptor)
    assert not policy_errors, policy_errors
    assert policy is not None
    assert policy["state"] == "READY_FOR_EXECUTOR_DESIGN"
    assert policy["recovery_capability_proven"] is True
    assert policy["production_publish_authorized"] is False
    assert policy["canonical_write_performed"] is False

    weak = passed_probe()
    weak["restart_recovery_verified"] = False
    weak_descriptor, weak_errors = derive_descriptor(encode(weak))
    assert weak_descriptor is None
    assert "probe_missing:restart_recovery_verified" in weak_errors

    dirty = passed_probe()
    dirty["cleanup_required"] = True
    dirty_descriptor, dirty_errors = derive_descriptor(encode(dirty))
    assert dirty_descriptor is None
    assert "probe_cleanup_required" in dirty_errors

    escaped = passed_probe()
    escaped["fixture_path"] = "00_LIBRARY/not-allowed"
    escaped_descriptor, escaped_errors = derive_descriptor(encode(escaped))
    assert escaped_descriptor is None
    assert "probe_fixture_path" in escaped_errors

    print(
        "candidate_remote_recovery_capability_descriptor_smoke_ok live_probe_required=1 "
        "recovery_descriptor=1 policy_ready_for_executor_design=1 private_scope=1 "
        "authorization_separate=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
