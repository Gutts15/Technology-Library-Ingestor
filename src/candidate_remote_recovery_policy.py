#!/usr/bin/env python3
"""Evaluate whether remote private recovery storage is ready for executor design.

This is a deterministic capability gate. It does not contact remote storage, does
not perform recovery, does not write 00_LIBRARY, and does not authorize publication.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
POLICY_VERSION = "0.1.0"

REQUIRED_TRUE = {
    "durable_journal_persistence",
    "snapshot_integrity_verified",
    "restart_independent_recovery",
    "byte_exact_rollback_verified",
    "terminal_recovery_state_verified",
    "private_recovery_scope",
    "cleanup_verified",
}


def evaluate_descriptor(payload: dict[str, Any] | None) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(payload, dict):
        return None, ["descriptor_invalid"]

    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("descriptor_schema")

    storage_id = payload.get("storage_id")
    if not isinstance(storage_id, str) or not storage_id.strip():
        errors.append("storage_id")

    recovery_kind = payload.get("recovery_kind")
    if not isinstance(recovery_kind, str) or not recovery_kind.strip():
        errors.append("recovery_kind")

    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence or any(not isinstance(item, str) or not item.strip() for item in evidence):
        errors.append("evidence_required")

    guarantees = payload.get("guarantees")
    if not isinstance(guarantees, dict):
        return None, sorted(set(errors + ["guarantees_invalid"]))

    invalid_guarantees = [
        key for key, value in guarantees.items()
        if not isinstance(key, str) or not isinstance(value, bool)
    ]
    if invalid_guarantees:
        errors.append("guarantee_values_must_be_boolean")

    if payload.get("production_publish_authorized") is not False:
        errors.append("descriptor_authorization_flag")
    if payload.get("canonical_write_performed") is not False:
        errors.append("descriptor_write_flag")

    if errors:
        return None, sorted(set(errors))

    reasons = [
        f"guarantee_missing:{field}"
        for field in sorted(REQUIRED_TRUE)
        if guarantees.get(field) is not True
    ]
    state = "READY_FOR_EXECUTOR_DESIGN" if not reasons else "BLOCKED"
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "storage_id": storage_id,
        "recovery_kind": recovery_kind,
        "state": state,
        "recovery_capability_proven": not reasons,
        "reasons": reasons,
        "evidence": evidence,
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }, []


def read_descriptor(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate remote recovery capabilities fail-closed.")
    parser.add_argument("--descriptor", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    report, errors = evaluate_descriptor(read_descriptor(args.descriptor))
    if errors or report is None:
        print(
            "candidate_remote_recovery_policy_error "
            f"codes={','.join(errors or ['evaluation_failed'])} canonical_write=0"
        )
        return 2

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "candidate_remote_recovery_policy_ok "
        f"state={report['state']} storage={report['storage_id']} "
        f"recovery_capability={1 if report['recovery_capability_proven'] else 0} "
        "authorized=0 canonical_write=0"
    )
    return 0 if report["state"] == "READY_FOR_EXECUTOR_DESIGN" else 3


if __name__ == "__main__":
    raise SystemExit(main())
