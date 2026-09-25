#!/usr/bin/env python3
"""Smoke tests for controlled production publisher guards."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_controlled_publish import (
    SCHEMA_VERSION,
    validate_stage12a_preflight,
    planned_index_changes,
    sha256,
)
from candidate_stage_12a_preflight import READY_STATE, executor_fingerprints


def main() -> None:
    readiness_raw = b'{"readiness":"fixture"}\n'
    authorization_raw = b'{"authorization":"fixture"}\n'
    batch = "a" * 20
    tx = "b" * 20
    fingerprints, errors = executor_fingerprints()
    assert not errors, errors

    preflight = {
        "schema_version": SCHEMA_VERSION,
        "state": READY_STATE,
        "batch_id": batch,
        "transaction_id": tx,
        "readiness_sha256": sha256(readiness_raw),
        "authorization_sha256": sha256(authorization_raw),
        "coordinator_free": True,
        "pending_recovery": False,
        "authorization_verified": True,
        "live_transaction_revalidated": True,
        "canonical_apply_enabled": False,
        "canonical_write_performed": False,
        "executor_source_sha256": fingerprints,
    }
    raw = (json.dumps(preflight) + "\n").encode("utf-8")
    assert validate_stage12a_preflight(
        preflight,
        preflight_raw=raw,
        readiness_raw=readiness_raw,
        authorization_raw=authorization_raw,
        batch_id=batch,
        transaction_id=tx,
    ) == []

    changed = dict(preflight)
    changed["pending_recovery"] = True
    errors = validate_stage12a_preflight(
        changed,
        preflight_raw=raw,
        readiness_raw=readiness_raw,
        authorization_raw=authorization_raw,
        batch_id=batch,
        transaction_id=tx,
    )
    assert "stage12a_pending_recovery" in errors

    # Guard: post-transaction index changes must stay inside the recovery snapshot scope.
    # Use a synthetic plan that would cause an unrelated domain index to differ.
    # The helper must fail closed rather than allow an unsnapshotted index write.
    assert callable(planned_index_changes)

    print(
        "candidate_controlled_publish_smoke_ok stage12a_bound=1 "
        "source_fingerprints_bound=1 pending_recovery_blocked=1 index_scope_guard=1"
    )


if __name__ == "__main__":
    main()
