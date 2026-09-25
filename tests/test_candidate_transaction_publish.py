#!/usr/bin/env python3
"""Smoke tests for the plan-only production publisher input gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_publish_authorization import build_authorization
from candidate_transaction_publish import evaluate_publish_inputs


def main() -> None:
    readiness = {
        "schema_version": 1,
        "readiness_version": "0.1.0",
        "state": "READY_FOR_PRODUCTION_EXECUTOR_IMPLEMENTATION",
        "batch_id": "a" * 20,
        "transaction_id": "b" * 20,
        "checks": {
            "live_batch_preconditions": True,
            "editorial_acceptance_bound": True,
            "structural_retrieval_preflight": True,
            "semantic_routing_quality": True,
            "transaction_plan_live_recomputed": True,
            "remote_coordination_capability": True,
            "remote_recovery_capability": True,
            "remote_success_path_fixture": True,
            "remote_restart_recovery_fixture": True,
        },
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }
    readiness_raw = (json.dumps(readiness, sort_keys=True) + "\n").encode("utf-8")
    authorization, auth_errors = build_authorization(
        readiness_raw,
        confirm_batch="a" * 20,
        confirm_transaction="b" * 20,
        confirm_phrase="AUTHORIZE_CANONICAL_WRITE",
    )
    assert not auth_errors and authorization is not None

    batch = {
        "state": "READY_FOR_EDITORIAL_REVIEW",
        "batch_id": "a" * 20,
        "live_preconditions_verified": True,
        "canonical_write_performed": False,
    }
    transaction = {
        "transaction_id": "b" * 20,
        "finalize_batch_id": "a" * 20,
        "live_preconditions_verified": True,
        "canonical_write_performed": False,
        "counts": {"writes": 5, "create": 5, "update": 0, "unchanged": 0},
    }

    errors = evaluate_publish_inputs(
        readiness_raw=readiness_raw,
        authorization=authorization,
        batch_report=batch,
        stored_transaction=transaction,
        recomputed_transaction=dict(transaction),
    )
    assert not errors, errors

    drifted = dict(transaction)
    drifted["transaction_id"] = "c" * 20
    errors = evaluate_publish_inputs(
        readiness_raw=readiness_raw,
        authorization=authorization,
        batch_report=batch,
        stored_transaction=transaction,
        recomputed_transaction=drifted,
    )
    assert "transaction_live_recompute_mismatch" in errors

    wrong_auth = dict(authorization)
    wrong_auth["transaction_id"] = "c" * 20
    errors = evaluate_publish_inputs(
        readiness_raw=readiness_raw,
        authorization=wrong_auth,
        batch_report=batch,
        stored_transaction=transaction,
        recomputed_transaction=dict(transaction),
    )
    assert "authorization_transaction_id" in errors

    print(
        "candidate_transaction_publish_smoke_ok plan_only=1 "
        "authorization_binding=1 live_transaction_binding=1 drift_blocked=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
