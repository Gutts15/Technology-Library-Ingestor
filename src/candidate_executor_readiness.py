#!/usr/bin/env python3
"""Deterministically gate readiness to implement the production candidate executor.

This module combines the live sealed batch/transaction state with already-produced
operator and integration evidence. It re-inspects the current batch, rebuilds the
virtual retrieval preflight, recomputes the live transaction plan, derives the
coordination/recovery policies directly from their raw live probe reports, and
checks the remote executor success/restart fixture reports.

A PASS means only READY_FOR_PRODUCTION_EXECUTOR_IMPLEMENTATION. It does not authorize
publication, does not write remote storage and does not write 00_LIBRARY.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from candidate_batch_editorial_acceptance import validate_acceptance
from candidate_batch_inspect import (
    DEFAULT_BATCH,
    DEFAULT_MASTER,
    DEFAULT_PUBLISH,
    DEFAULT_ROOT,
    DEFAULT_UPDATE,
    DEFAULT_VALIDATION,
    inspect_batch,
    read_json_bytes,
    read_local,
    read_remote,
)
from candidate_git_ref_capability_descriptor import derive_descriptor as derive_coord_descriptor
from candidate_remote_coordination_policy import evaluate_descriptor as evaluate_coord_policy
from candidate_remote_recovery_capability_descriptor import derive_descriptor as derive_recovery_descriptor
from candidate_remote_recovery_policy import evaluate_descriptor as evaluate_recovery_policy
from candidate_retrieval_preflight import build_preflight_report
from candidate_retrieval_routing_local import build_virtual_entries
from candidate_transaction_prepare import (
    DEFAULT_TRANSACTION_READY,
    parse_update_manifest,
    prepare_transaction,
)
from library_index_build import local_record_paths, remote_record_paths

SCHEMA_VERSION = 1
READINESS_VERSION = "0.1.1"
READY_STATE = "READY_FOR_PRODUCTION_EXECUTOR_IMPLEMENTATION"


def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _false_safety_flags(payload: dict[str, Any] | None, prefix: str) -> list[str]:
    if not isinstance(payload, dict):
        return [f"{prefix}_invalid"]
    errors: list[str] = []
    if payload.get("production_publish_authorized") is not False:
        errors.append(f"{prefix}_authorization_flag")
    if payload.get("canonical_write_performed") is not False:
        errors.append(f"{prefix}_write_flag")
    return errors


def evaluate_readiness(
    *,
    batch_report: dict[str, Any] | None,
    acceptance: dict[str, Any] | None,
    preflight: dict[str, Any] | None,
    stored_transaction: dict[str, Any] | None,
    recomputed_transaction: dict[str, Any] | None,
    routing: dict[str, Any] | None,
    routing_virtual_record_count: int | None,
    coordination_policy: dict[str, Any] | None,
    recovery_policy: dict[str, Any] | None,
    wiring_fixture: dict[str, Any] | None,
    restart_recovery_fixture: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(batch_report, dict):
        return None, ["batch_report_invalid"]
    errors: list[str] = []
    batch_id = batch_report.get("batch_id")

    if batch_report.get("state") != "READY_FOR_EDITORIAL_REVIEW":
        errors.append("batch_state")
    if batch_report.get("live_preconditions_verified") is not True:
        errors.append("batch_live_preconditions")
    if batch_report.get("canonical_write_performed") is not False:
        errors.append("batch_write_flag")

    errors.extend(validate_acceptance(acceptance, batch_report))

    if not isinstance(preflight, dict):
        errors.append("preflight_invalid")
    else:
        if preflight.get("state") != "PASS":
            errors.append("preflight_state")
        if preflight.get("batch_id") != batch_id:
            errors.append("preflight_batch_binding")
        if preflight.get("failed_case_ids") != []:
            errors.append("preflight_failed_cases")
        if preflight.get("canonical_write_performed") is not False:
            errors.append("preflight_write_flag")

    if not isinstance(stored_transaction, dict) or not isinstance(recomputed_transaction, dict):
        errors.append("transaction_missing")
    else:
        if stored_transaction != recomputed_transaction:
            errors.append("transaction_live_recompute_mismatch")
        if stored_transaction.get("finalize_batch_id") != batch_id:
            errors.append("transaction_batch_binding")
        if stored_transaction.get("live_preconditions_verified") is not True:
            errors.append("transaction_live_preconditions")
        if stored_transaction.get("canonical_write_performed") is not False:
            errors.append("transaction_write_flag")
        transaction_id = stored_transaction.get("transaction_id")
        if not isinstance(transaction_id, str) or len(transaction_id) != 20:
            errors.append("transaction_id")

    if not isinstance(routing, dict):
        errors.append("routing_invalid")
    else:
        if routing.get("state") != "PASS":
            errors.append("routing_state")
        if routing.get("batch_id") != batch_id:
            errors.append("routing_batch_binding")
        if routing.get("routing_quality_proven") is not True:
            errors.append("routing_not_proven")
        if routing.get("failed_case_ids") != []:
            errors.append("routing_failed_cases")
        if not isinstance(routing_virtual_record_count, int) or routing_virtual_record_count < 0:
            errors.append("routing_virtual_count_expected_invalid")
        elif routing.get("virtual_record_count") != routing_virtual_record_count:
            errors.append("routing_virtual_count_binding")
        errors.extend(_false_safety_flags(routing, "routing"))

    if not isinstance(coordination_policy, dict):
        errors.append("coordination_policy_invalid")
    else:
        if coordination_policy.get("state") != "READY_FOR_EXECUTOR_DESIGN":
            errors.append("coordination_policy_state")
        if coordination_policy.get("exclusive_acquire_proven") is not True:
            errors.append("coordination_exclusive_acquire")
        errors.extend(_false_safety_flags(coordination_policy, "coordination"))

    if not isinstance(recovery_policy, dict):
        errors.append("recovery_policy_invalid")
    else:
        if recovery_policy.get("state") != "READY_FOR_EXECUTOR_DESIGN":
            errors.append("recovery_policy_state")
        if recovery_policy.get("recovery_capability_proven") is not True:
            errors.append("recovery_capability")
        errors.extend(_false_safety_flags(recovery_policy, "recovery_policy"))

    wiring_true = {
        "fixture_seed_verified",
        "coordinator_acquired",
        "live_preconditions_revalidated",
        "recovery_journal_verified",
        "write_ready_verified",
        "synthetic_transaction_verified",
        "receipt_verified",
        "journal_committed",
        "coordinator_released",
        "cleanup_verified",
    }
    if not isinstance(wiring_fixture, dict):
        errors.append("wiring_fixture_invalid")
    else:
        if wiring_fixture.get("state") != "PASS":
            errors.append("wiring_fixture_state")
        for field in sorted(wiring_true):
            if wiring_fixture.get(field) is not True:
                errors.append(f"wiring_fixture_missing:{field}")
        if wiring_fixture.get("cleanup_required") is not False:
            errors.append("wiring_fixture_cleanup_required")
        errors.extend(_false_safety_flags(wiring_fixture, "wiring_fixture"))

    restart_true = {
        "coordinator_acquired",
        "preconditions_revalidated",
        "journal_verified",
        "interrupted_state_verified",
        "fresh_process_recovery_verified",
        "rollback_verified",
        "coordinator_released_after_recovery",
        "cleanup_verified",
    }
    if not isinstance(restart_recovery_fixture, dict):
        errors.append("restart_recovery_fixture_invalid")
    else:
        if restart_recovery_fixture.get("state") != "PASS":
            errors.append("restart_recovery_fixture_state")
        for field in sorted(restart_true):
            if restart_recovery_fixture.get(field) is not True:
                errors.append(f"restart_recovery_fixture_missing:{field}")
        if restart_recovery_fixture.get("cleanup_required") is not False:
            errors.append("restart_recovery_fixture_cleanup_required")
        errors.extend(_false_safety_flags(restart_recovery_fixture, "restart_recovery_fixture"))

    if errors:
        return None, sorted(set(errors))

    transaction_id = str(stored_transaction.get("transaction_id"))
    return {
        "schema_version": SCHEMA_VERSION,
        "readiness_version": READINESS_VERSION,
        "state": READY_STATE,
        "batch_id": batch_id,
        "transaction_id": transaction_id,
        "virtual_record_count": preflight.get("virtual_record_count") if isinstance(preflight, dict) else None,
        "routing_virtual_record_count": routing_virtual_record_count,
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
    }, []


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate readiness to implement, but not authorize, the production candidate executor.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root-dir", type=Path)
    mode.add_argument("--remote")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--cases", type=Path, default=Path(__file__).resolve().parents[1] / "tests" / "retrieval_regression_cases.json")
    parser.add_argument("--editorial-acceptance", type=Path, required=True)
    parser.add_argument("--routing-report", type=Path, required=True)
    parser.add_argument("--coordination-probe", type=Path, required=True)
    parser.add_argument("--recovery-probe", type=Path, required=True)
    parser.add_argument("--wiring-fixture-report", type=Path, required=True)
    parser.add_argument("--restart-recovery-report", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    root_base = args.root.strip("/")
    publish_rel = f"{root_base}/{DEFAULT_PUBLISH}"
    validation_rel = f"{root_base}/{DEFAULT_VALIDATION}"
    update_rel = f"{root_base}/{DEFAULT_UPDATE}"
    batch_rel = f"{root_base}/{DEFAULT_BATCH}"
    transaction_rel = f"{root_base}/{DEFAULT_TRANSACTION_READY}"

    if args.root_dir is not None:
        root = args.root_dir.resolve()
        reader = lambda relative: read_local(root, relative)
        canonical_paths = local_record_paths(root)
    else:
        if not shutil.which("rclone"):
            print("candidate_executor_readiness_error code=rclone_missing canonical_write=0")
            return 2
        remote = str(args.remote)
        reader = lambda relative: read_remote(remote, relative)
        canonical_paths = remote_record_paths(remote)

    master_raw = reader(DEFAULT_MASTER)
    publish_raw = reader(publish_rel)
    validation_raw = reader(validation_rel)
    update_raw = reader(update_rel)
    batch_raw = reader(batch_rel)
    transaction_raw = reader(transaction_rel)

    batch_report, batch_errors = inspect_batch(
        master_raw=master_raw,
        publish_raw=publish_raw,
        validation_raw=validation_raw,
        update_raw=update_raw,
        batch_raw=batch_raw,
        read_content=reader,
    )
    if batch_errors or batch_report is None or master_raw is None:
        print(
            "candidate_executor_readiness_error "
            f"codes={','.join(batch_errors or ['batch_inspection_failed'])} canonical_write=0"
        )
        return 2

    try:
        cases_payload = json.loads(args.cases.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        print("candidate_executor_readiness_error code=retrieval_cases_read_failed canonical_write=0")
        return 2
    preflight, preflight_errors = build_preflight_report(
        canonical_paths=canonical_paths,
        batch_report=batch_report,
        cases_payload=cases_payload if isinstance(cases_payload, dict) else None,
        read_content=reader,
    )
    if preflight_errors or preflight is None:
        print(
            "candidate_executor_readiness_error "
            f"codes={','.join(preflight_errors or ['preflight_failed'])} canonical_write=0"
        )
        return 2

    routing_entries, routing_entry_errors = build_virtual_entries(canonical_paths, batch_report, reader)
    if routing_entry_errors:
        print(
            "candidate_executor_readiness_error "
            f"codes={','.join(routing_entry_errors)} canonical_write=0"
        )
        return 2
    routing_virtual_record_count = len(routing_entries)

    publish = read_json_bytes(publish_raw)
    validation = read_json_bytes(validation_raw)
    update_manifest = read_json_bytes(update_raw)
    updates, update_errors = parse_update_manifest(update_manifest)
    if update_errors:
        print(
            "candidate_executor_readiness_error "
            f"codes={','.join(update_errors)} canonical_write=0"
        )
        return 2
    recomputed_transaction, transaction_errors = prepare_transaction(
        master_content=master_raw,
        new_manifest=publish,
        new_validation=validation,
        update_artifacts=updates,
        read_content=reader,
        finalize_batch_id=str(batch_report.get("batch_id")),
    )
    if transaction_errors or recomputed_transaction is None:
        print(
            "candidate_executor_readiness_error "
            f"codes={','.join(transaction_errors or ['transaction_recompute_failed'])} canonical_write=0"
        )
        return 2
    stored_transaction = read_json_bytes(transaction_raw)

    try:
        coordination_probe_raw = args.coordination_probe.read_bytes()
        recovery_probe_raw = args.recovery_probe.read_bytes()
    except OSError:
        print("candidate_executor_readiness_error code=probe_report_missing canonical_write=0")
        return 2
    coord_descriptor, coord_errors = derive_coord_descriptor(coordination_probe_raw)
    recovery_descriptor, recovery_descriptor_errors = derive_recovery_descriptor(recovery_probe_raw)
    if coord_errors or coord_descriptor is None or recovery_descriptor_errors or recovery_descriptor is None:
        codes = [*(f"coord:{code}" for code in coord_errors), *(f"recovery:{code}" for code in recovery_descriptor_errors)]
        print(f"candidate_executor_readiness_error codes={','.join(codes or ['descriptor_failed'])} canonical_write=0")
        return 2
    coordination_policy, coordination_errors = evaluate_coord_policy(coord_descriptor)
    recovery_policy, recovery_policy_errors = evaluate_recovery_policy(recovery_descriptor)
    if coordination_errors or coordination_policy is None or recovery_policy_errors or recovery_policy is None:
        codes = [*(f"coord_policy:{code}" for code in coordination_errors), *(f"recovery_policy:{code}" for code in recovery_policy_errors)]
        print(f"candidate_executor_readiness_error codes={','.join(codes or ['policy_failed'])} canonical_write=0")
        return 2

    report, errors = evaluate_readiness(
        batch_report=batch_report,
        acceptance=read_json_file(args.editorial_acceptance),
        preflight=preflight,
        stored_transaction=stored_transaction,
        recomputed_transaction=recomputed_transaction,
        routing=read_json_file(args.routing_report),
        routing_virtual_record_count=routing_virtual_record_count,
        coordination_policy=coordination_policy,
        recovery_policy=recovery_policy,
        wiring_fixture=read_json_file(args.wiring_fixture_report),
        restart_recovery_fixture=read_json_file(args.restart_recovery_report),
    )
    if errors or report is None:
        print(
            "candidate_executor_readiness_error "
            f"codes={','.join(errors or ['readiness_failed'])} authorized=0 canonical_write=0"
        )
        return 3

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_executor_readiness_ok "
        f"state={report['state']} batch_id={report['batch_id']} transaction_id={report['transaction_id']} "
        "checks=9 authorized=0 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
