#!/usr/bin/env python3
"""Smoke tests for live-probe to Git exact-ref CAS capability descriptor derivation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_git_ref_capability_descriptor import COORDINATOR_ID, derive_descriptor
from candidate_remote_coordination_policy import evaluate_descriptor


def passed_probe() -> dict:
    return {
        "schema_version": 1,
        "probe_version": "0.2.0",
        "state": "PASS",
        "fixture_ref": "refs/heads/tl-coordination-fixture-123456789abc",
        "single_winner": True,
        "winner": "probe-owner-a",
        "restart_state_read": True,
        "recovery_verified": True,
        "stale_owner_blocked": True,
        "restarted_owner_acquired": True,
        "owner_release_verified": True,
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
    assert descriptor["coordinator_id"] == COORDINATOR_ID
    assert descriptor["coordination_kind"] == "COMPARE_AND_SWAP"
    assert descriptor["guarantees"]["compare_and_swap"] is True
    assert descriptor["guarantees"]["read_after_write_consistency"] is True
    assert descriptor["guarantees"]["durable_owner_identity"] is True
    assert descriptor["guarantees"]["transaction_scoped_ownership"] is True
    assert descriptor["guarantees"]["conflict_detection"] is True
    assert descriptor["guarantees"]["no_silent_overwrite"] is True
    assert descriptor["guarantees"]["stale_owner_requires_verified_recovery"] is True
    assert descriptor["guarantees"]["release_requires_owner_or_verified_recovery"] is True
    assert descriptor["production_publish_authorized"] is False
    assert descriptor["canonical_write_performed"] is False

    policy, policy_errors = evaluate_descriptor(descriptor)
    assert not policy_errors, policy_errors
    assert policy is not None
    assert policy["state"] == "READY_FOR_EXECUTOR_DESIGN"
    assert policy["exclusive_acquire_proven"] is True
    assert policy["production_publish_authorized"] is False
    assert policy["canonical_write_performed"] is False

    stale = passed_probe()
    stale["probe_version"] = "0.1.0"
    stale_descriptor, stale_errors = derive_descriptor(encode(stale))
    assert stale_descriptor is None
    assert "probe_version_too_old" in stale_errors

    weak = passed_probe()
    weak["stale_owner_blocked"] = False
    weak_descriptor, weak_errors = derive_descriptor(encode(weak))
    assert weak_descriptor is None
    assert "probe_missing:stale_owner_blocked" in weak_errors

    dirty = passed_probe()
    dirty["cleanup_required"] = True
    dirty_descriptor, dirty_errors = derive_descriptor(encode(dirty))
    assert dirty_descriptor is None
    assert "probe_cleanup_required" in dirty_errors

    print(
        "candidate_git_ref_capability_descriptor_smoke_ok live_probe_required=1 "
        "cas_descriptor=1 policy_ready_for_executor_design=1 authorization_separate=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
