#!/usr/bin/env python3
"""Exercise the full candidate transaction sequence inside a disposable fixture.

This orchestration layer composes the already-tested fixture lease, durable recovery
journal, transaction executor, verified receipt and lease-finalization rules. It is
still local-fixture-only and exists to prove sequencing before any remote production
publisher is implemented.

Success path:
  acquire lease -> prepare durable journal -> execute + verify transaction ->
  mark journal COMMITTED from verified receipt -> release lease.

Failure path after execution begins:
  fixture executor rolls back in-process -> durable journal recovery re-verifies the
  original state -> stale lease is released only from RECOVERED + verified rollback.

No remote mode exists and canonical_write_performed is always false because this
module can operate only under the explicit test-fixture marker.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from candidate_transaction_fixture import simulate_transaction
from candidate_transaction_lease_fixture import (
    acquire_lease,
    lease_path,
    release_lease,
    release_recovered_lease,
)
from candidate_transaction_plan import read_json
from candidate_transaction_recovery_fixture import (
    journal_dir,
    mark_journal_committed,
    prepare_recovery_journal,
    recover_from_journal,
)

SCHEMA_VERSION = 1
ORCHESTRATOR_VERSION = "0.1.0"


def run_fixture_transaction(
    root: Path,
    plan: dict[str, Any] | None,
    *,
    owner: str,
    fail_after_writes: int | None = None,
    fail_after_index_rebuild: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    root = root.resolve()
    if not isinstance(plan, dict):
        return None, ["transaction_plan_invalid"]
    transaction_id = plan.get("transaction_id")
    if not isinstance(transaction_id, str) or len(transaction_id) != 20:
        return None, ["transaction_id_invalid"]

    lease, lease_errors = acquire_lease(root, transaction_id, owner)
    if lease_errors or lease is None:
        return None, [f"lease:{code}" for code in lease_errors or ["acquire_failed"]]

    journal, journal_errors = prepare_recovery_journal(root, plan)
    if journal_errors or journal is None:
        # If no durable journal exists, no canonical mutation is authorized. The
        # owner may release only when there is truly no prepared journal.
        jpath = journal_dir(root, transaction_id) / "journal.json"
        release_errors: list[str] = []
        if not jpath.exists():
            release_errors = release_lease(root, transaction_id, owner)
        return {
            "schema_version": SCHEMA_VERSION,
            "orchestrator_version": ORCHESTRATOR_VERSION,
            "transaction_id": transaction_id,
            "state": "PREPARE_FAILED",
            "lease_active": lease_path(root).exists(),
            "journal_prepared": False,
            "canonical_write_performed": False,
        }, sorted(set([*(f"journal:{code}" for code in journal_errors), *(f"release:{code}" for code in release_errors)]))

    transaction_report, transaction_errors = simulate_transaction(
        root,
        plan,
        fail_after_writes=fail_after_writes,
        fail_after_index_rebuild=fail_after_index_rebuild,
    )
    if transaction_errors or not isinstance(transaction_report, dict):
        recovered, recovery_errors = recover_from_journal(root, transaction_id)
        release_errors: list[str] = []
        if not recovery_errors and isinstance(recovered, dict) and recovered.get("state") == "RECOVERED":
            release_errors = release_recovered_lease(root, transaction_id)
        errors = [
            *(f"transaction:{code}" for code in transaction_errors or ["execution_failed"]),
            *(f"recovery:{code}" for code in recovery_errors),
            *(f"release:{code}" for code in release_errors),
        ]
        state = "RECOVERED_AFTER_FAILURE" if not recovery_errors and not release_errors else "RECOVERY_REQUIRED"
        return {
            "schema_version": SCHEMA_VERSION,
            "orchestrator_version": ORCHESTRATOR_VERSION,
            "transaction_id": transaction_id,
            "state": state,
            "transaction_state": transaction_report.get("state") if isinstance(transaction_report, dict) else None,
            "journal_state": recovered.get("state") if isinstance(recovered, dict) else None,
            "rollback_verified": bool(isinstance(recovered, dict) and recovered.get("rollback_verified") is True),
            "lease_active": lease_path(root).exists(),
            "canonical_write_performed": False,
        }, sorted(set(errors))

    receipt = transaction_report.get("receipt")
    committed, commit_errors = mark_journal_committed(root, transaction_id, receipt if isinstance(receipt, dict) else None)
    if commit_errors or committed is None:
        # Canonical fixture state may already be committed, so never release the
        # owner here. A later operator must inspect/recover using durable state.
        return {
            "schema_version": SCHEMA_VERSION,
            "orchestrator_version": ORCHESTRATOR_VERSION,
            "transaction_id": transaction_id,
            "state": "COMMITTED_PENDING_JOURNAL_FINALIZATION",
            "transaction_state": transaction_report.get("state"),
            "receipt_verified": transaction_report.get("receipt_verified") is True,
            "lease_active": lease_path(root).exists(),
            "canonical_write_performed": False,
        }, [f"journal_commit:{code}" for code in commit_errors or ["failed"]]

    release_errors = release_lease(root, transaction_id, owner)
    if release_errors:
        return {
            "schema_version": SCHEMA_VERSION,
            "orchestrator_version": ORCHESTRATOR_VERSION,
            "transaction_id": transaction_id,
            "state": "COMMITTED_LEASE_RELEASE_FAILED",
            "transaction_state": transaction_report.get("state"),
            "journal_state": committed.get("state"),
            "receipt_verified": committed.get("receipt_verified") is True,
            "lease_active": lease_path(root).exists(),
            "canonical_write_performed": False,
        }, [f"release:{code}" for code in release_errors]

    return {
        "schema_version": SCHEMA_VERSION,
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "transaction_id": transaction_id,
        "state": "COMMITTED_FIXTURE_FULL_SEQUENCE",
        "transaction_state": transaction_report.get("state"),
        "journal_state": committed.get("state"),
        "receipt_verified": committed.get("receipt_verified") is True,
        "lease_active": False,
        "journal_path": str(journal_dir(root, transaction_id) / "journal.json"),
        "canonical_write_performed": False,
    }, []


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the full candidate transaction sequence in a local fixture only.")
    parser.add_argument("--root-dir", type=Path, required=True)
    parser.add_argument("--transaction-plan", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--fail-after-writes", type=int)
    parser.add_argument("--fail-after-index-rebuild", action="store_true")
    parser.add_argument("--report-out", type=Path)
    args = parser.parse_args()

    report, errors = run_fixture_transaction(
        args.root_dir,
        read_json(args.transaction_plan),
        owner=args.owner,
        fail_after_writes=args.fail_after_writes,
        fail_after_index_rebuild=args.fail_after_index_rebuild,
    )
    if args.report_out is not None and report is not None:
        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        args.report_out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if errors or report is None:
        state = report.get("state") if isinstance(report, dict) else "FAILED"
        print(
            "candidate_transaction_orchestrator_fixture_error "
            f"state={state} codes={','.join(errors or ['failed'])} canonical_write=0"
        )
        return 2

    print(
        "candidate_transaction_orchestrator_fixture_ok "
        f"state={report['state']} transaction_id={report['transaction_id']} "
        f"receipt_verified={1 if report.get('receipt_verified') else 0} lease_active=0 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
