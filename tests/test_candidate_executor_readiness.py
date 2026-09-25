#!/usr/bin/env python3
"""Smoke tests for deterministic candidate executor readiness."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_batch_editorial_acceptance import build_acceptance
from candidate_executor_readiness import READY_STATE, evaluate_readiness


def batch_report() -> dict:
    return {
        "state": "READY_FOR_EDITORIAL_REVIEW",
        "batch_id": "dba704d20a5ed3f6915c",
        "master_index_sha256": "a" * 64,
        "live_preconditions_verified": True,
        "canonical_write_performed": False,
        "counts": {"items": 1, "create": 1, "update": 0, "unchanged": 0},
        "items": [
            {
                "title": "Example",
                "record_id": "b" * 20,
                "action": "CREATE",
                "target_path": "00_LIBRARY/Example.md",
                "content_sha256": "c" * 64,
            }
        ],
    }


def main() -> None:
    batch = batch_report()
    acceptance, acceptance_errors = build_acceptance(batch)
    assert not acceptance_errors and acceptance is not None

    preflight = {
        "state": "PASS",
        "batch_id": batch["batch_id"],
        "failed_case_ids": [],
        "virtual_record_count": 31,
        "canonical_write_performed": False,
    }
    transaction = {
        "transaction_id": "e" * 20,
        "finalize_batch_id": batch["batch_id"],
        "live_preconditions_verified": True,
        "canonical_write_performed": False,
    }
    routing = {
        "state": "PASS",
        "batch_id": batch["batch_id"],
        "routing_quality_proven": True,
        "failed_case_ids": [],
        # Routing intentionally excludes SOURCE records, so this count need not
        # equal the structural preflight's all-record virtual count.
        "virtual_record_count": 15,
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }
    routing_virtual_record_count = 15
    coordination = {
        "state": "READY_FOR_EXECUTOR_DESIGN",
        "exclusive_acquire_proven": True,
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }
    recovery_policy = {
        "state": "READY_FOR_EXECUTOR_DESIGN",
        "recovery_capability_proven": True,
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }
    wiring = {
        "state": "PASS",
        "fixture_seed_verified": True,
        "coordinator_acquired": True,
        "live_preconditions_revalidated": True,
        "recovery_journal_verified": True,
        "write_ready_verified": True,
        "synthetic_transaction_verified": True,
        "receipt_verified": True,
        "journal_committed": True,
        "coordinator_released": True,
        "cleanup_verified": True,
        "cleanup_required": False,
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }
    restart = {
        "state": "PASS",
        "coordinator_acquired": True,
        "preconditions_revalidated": True,
        "journal_verified": True,
        "interrupted_state_verified": True,
        "fresh_process_recovery_verified": True,
        "rollback_verified": True,
        "coordinator_released_after_recovery": True,
        "cleanup_verified": True,
        "cleanup_required": False,
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }

    report, errors = evaluate_readiness(
        batch_report=batch,
        acceptance=acceptance,
        preflight=preflight,
        stored_transaction=transaction,
        recomputed_transaction=dict(transaction),
        routing=routing,
        routing_virtual_record_count=routing_virtual_record_count,
        coordination_policy=coordination,
        recovery_policy=recovery_policy,
        wiring_fixture=wiring,
        restart_recovery_fixture=restart,
    )
    assert not errors and report is not None, errors
    assert report["state"] == READY_STATE
    assert report["virtual_record_count"] == 31
    assert report["routing_virtual_record_count"] == 15
    assert report["production_publish_authorized"] is False
    assert report["canonical_write_performed"] is False
    assert all(report["checks"].values())

    bad_routing = dict(routing)
    bad_routing["routing_quality_proven"] = False
    failed, fail_errors = evaluate_readiness(
        batch_report=batch,
        acceptance=acceptance,
        preflight=preflight,
        stored_transaction=transaction,
        recomputed_transaction=dict(transaction),
        routing=bad_routing,
        routing_virtual_record_count=routing_virtual_record_count,
        coordination_policy=coordination,
        recovery_policy=recovery_policy,
        wiring_fixture=wiring,
        restart_recovery_fixture=restart,
    )
    assert failed is None
    assert "routing_not_proven" in fail_errors

    bad_count = dict(routing)
    bad_count["virtual_record_count"] = 31
    failed, fail_errors = evaluate_readiness(
        batch_report=batch,
        acceptance=acceptance,
        preflight=preflight,
        stored_transaction=transaction,
        recomputed_transaction=dict(transaction),
        routing=bad_count,
        routing_virtual_record_count=routing_virtual_record_count,
        coordination_policy=coordination,
        recovery_policy=recovery_policy,
        wiring_fixture=wiring,
        restart_recovery_fixture=restart,
    )
    assert failed is None
    assert "routing_virtual_count_binding" in fail_errors

    drifted_transaction = dict(transaction)
    drifted_transaction["transaction_id"] = "f" * 20
    failed, fail_errors = evaluate_readiness(
        batch_report=batch,
        acceptance=acceptance,
        preflight=preflight,
        stored_transaction=transaction,
        recomputed_transaction=drifted_transaction,
        routing=routing,
        routing_virtual_record_count=routing_virtual_record_count,
        coordination_policy=coordination,
        recovery_policy=recovery_policy,
        wiring_fixture=wiring,
        restart_recovery_fixture=restart,
    )
    assert failed is None
    assert "transaction_live_recompute_mismatch" in fail_errors

    print(
        "candidate_executor_readiness_smoke_ok gates=9 editorial_binding=1 "
        "live_transaction_recompute=1 routing_required=1 routing_catalog_binding=1 "
        "remote_fixtures_required=1 authorization_separate=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
