#!/usr/bin/env python3
"""Evaluate whether a remote transaction coordinator is safe enough for production.

Candidate publication needs stronger semantics than ordinary remote file copy/move.
This module is a deterministic capability gate. It does not acquire locks, does not
contact storage, and does not enable publication. A future production executor may
only be wired to a coordinator whose independently established capability descriptor
passes this policy.

The current rclone + Google Drive path-lock idea is represented explicitly as a
blocked profile: normal path operations overwrite existing names and Drive permits
duplicate names, so path existence cannot be treated as compare-and-swap.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
POLICY_VERSION = "0.1.0"

REQUIRED_TRUE = {
    "read_after_write_consistency",
    "durable_owner_identity",
    "transaction_scoped_ownership",
    "conflict_detection",
    "no_silent_overwrite",
    "stale_owner_requires_verified_recovery",
    "release_requires_owner_or_verified_recovery",
}


def builtin_profile(name: str) -> dict[str, Any] | None:
    if name != "rclone-google-drive-path-lock":
        return None
    return {
        "schema_version": SCHEMA_VERSION,
        "descriptor_version": "0.1.0",
        "coordinator_id": "rclone-google-drive-path-lock",
        "coordination_kind": "REMOTE_PATH_SENTINEL",
        "evidence": [
            "https://rclone.org/commands/rclone_copyto/",
            "https://rclone.org/commands/rclone_moveto/",
            "https://rclone.org/drive/#duplicated-files",
        ],
        "guarantees": {
            "atomic_create_if_absent": False,
            "compare_and_swap": False,
            "read_after_write_consistency": False,
            "durable_owner_identity": True,
            "transaction_scoped_ownership": True,
            "conflict_detection": False,
            "no_silent_overwrite": False,
            "stale_owner_requires_verified_recovery": True,
            "release_requires_owner_or_verified_recovery": True,
        },
        "notes": "Ordinary rclone path operations are not a proven exclusive CAS/lease primitive for Google Drive.",
    }


def evaluate_descriptor(payload: dict[str, Any] | None) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(payload, dict):
        return None, ["descriptor_invalid"]
    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("descriptor_schema")
    coordinator_id = payload.get("coordinator_id")
    if not isinstance(coordinator_id, str) or not coordinator_id.strip():
        errors.append("coordinator_id")
    kind = payload.get("coordination_kind")
    if not isinstance(kind, str) or not kind.strip():
        errors.append("coordination_kind")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence or any(not isinstance(item, str) or not item.strip() for item in evidence):
        errors.append("evidence_required")
    guarantees = payload.get("guarantees")
    if not isinstance(guarantees, dict):
        return None, sorted(set(errors + ["guarantees_invalid"]))

    acquisition = bool(guarantees.get("atomic_create_if_absent")) or bool(guarantees.get("compare_and_swap"))
    reasons: list[str] = []
    if not acquisition:
        reasons.append("exclusive_acquire_not_proven")
    for field in sorted(REQUIRED_TRUE):
        if guarantees.get(field) is not True:
            reasons.append(f"guarantee_missing:{field}")

    unknown_guarantees = [
        key for key, value in guarantees.items()
        if not isinstance(key, str) or not isinstance(value, bool)
    ]
    if unknown_guarantees:
        errors.append("guarantee_values_must_be_boolean")

    if errors:
        return None, sorted(set(errors))

    state = "READY_FOR_EXECUTOR_DESIGN" if not reasons else "BLOCKED"
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "coordinator_id": coordinator_id,
        "coordination_kind": kind,
        "state": state,
        "exclusive_acquire_proven": acquisition,
        "reasons": reasons,
        "evidence": evidence,
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }, []


def read_descriptor(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate remote transaction coordination capabilities fail-closed.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--descriptor", type=Path)
    source.add_argument("--profile", choices=["rclone-google-drive-path-lock"])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    payload = builtin_profile(str(args.profile)) if args.profile else read_descriptor(args.descriptor)
    report, errors = evaluate_descriptor(payload)
    if errors or report is None:
        print(
            "candidate_remote_coordination_policy_error "
            f"codes={','.join(errors or ['evaluation_failed'])} canonical_write=0"
        )
        return 2

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "candidate_remote_coordination_policy_ok "
        f"state={report['state']} coordinator={report['coordinator_id']} "
        f"exclusive_acquire={1 if report['exclusive_acquire_proven'] else 0} "
        f"authorized=0 canonical_write=0"
    )
    return 0 if report["state"] == "READY_FOR_EXECUTOR_DESIGN" else 3


if __name__ == "__main__":
    raise SystemExit(main())
