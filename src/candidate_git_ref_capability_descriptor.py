#!/usr/bin/env python3
"""Derive a production-coordination capability descriptor from a passed live Git-ref probe.

This helper is fail-closed. It consumes only the JSON report emitted by
``candidate_git_ref_remote_probe.py`` V0.2.0 or later, requires every coordination
property currently exercised by that probe, and emits a descriptor suitable for
``candidate_remote_coordination_policy.py --descriptor``.

It does not contact GitHub, does not mutate remote refs, does not write 00_LIBRARY,
and does not authorize publication. A passing descriptor can yield only
READY_FOR_EXECUTOR_DESIGN when evaluated by the separate policy gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DESCRIPTOR_VERSION = "0.1.0"
MIN_PROBE_VERSION = (0, 2, 0)
COORDINATOR_ID = "github-git-exact-ref-cas"
COORDINATION_KIND = "COMPARE_AND_SWAP"

REQUIRED_TRUE = {
    "single_winner",
    "restart_state_read",
    "recovery_verified",
    "stale_owner_blocked",
    "restarted_owner_acquired",
    "owner_release_verified",
    "cleanup_verified",
}


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def parse_version(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        parsed = tuple(int(part) for part in parts)
    except ValueError:
        return None
    if any(part < 0 for part in parsed):
        return None
    return parsed  # type: ignore[return-value]


def derive_descriptor(raw: bytes) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, ["probe_report_invalid_json"]
    if not isinstance(payload, dict):
        return None, ["probe_report_invalid"]

    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("probe_schema")
    version = parse_version(payload.get("probe_version"))
    if version is None or version < MIN_PROBE_VERSION:
        errors.append("probe_version_too_old")
    if payload.get("state") != "PASS":
        errors.append("probe_not_passed")
    for field in sorted(REQUIRED_TRUE):
        if payload.get(field) is not True:
            errors.append(f"probe_missing:{field}")
    if payload.get("cleanup_required") is not False:
        errors.append("probe_cleanup_required")
    if payload.get("production_publish_authorized") is not False:
        errors.append("probe_authorization_flag")
    if payload.get("canonical_write_performed") is not False:
        errors.append("probe_write_flag")

    fixture_ref = payload.get("fixture_ref")
    if not isinstance(fixture_ref, str) or not fixture_ref.startswith("refs/heads/tl-coordination-fixture-"):
        errors.append("probe_fixture_ref")

    if errors:
        return None, sorted(set(errors))

    report_sha = sha256(raw)
    descriptor = {
        "schema_version": SCHEMA_VERSION,
        "descriptor_version": DESCRIPTOR_VERSION,
        "coordinator_id": COORDINATOR_ID,
        "coordination_kind": COORDINATION_KIND,
        "evidence": [
            f"live_probe_sha256:{report_sha}",
            f"live_probe_version:{payload['probe_version']}",
            f"disposable_fixture_ref:{fixture_ref}",
            "live_probe:single_winner",
            "live_probe:restart_state_read",
            "live_probe:verified_recovery",
            "live_probe:stale_owner_blocked",
            "live_probe:owner_release_verified",
            "live_probe:cleanup_verified",
        ],
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
        "source_probe": {
            "report_sha256": report_sha,
            "probe_version": payload["probe_version"],
            "fixture_ref": fixture_ref,
            "winner": payload.get("winner"),
        },
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }
    return descriptor, []


def main() -> int:
    parser = argparse.ArgumentParser(description="Derive a Git exact-ref CAS capability descriptor from a passed live probe report.")
    parser.add_argument("--probe-report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    try:
        raw = args.probe_report.read_bytes()
    except OSError:
        print("candidate_git_ref_capability_descriptor_error code=probe_report_missing canonical_write=0")
        return 2

    descriptor, errors = derive_descriptor(raw)
    if errors or descriptor is None:
        print(
            "candidate_git_ref_capability_descriptor_error "
            f"codes={','.join(errors or ['derive_failed'])} canonical_write=0"
        )
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_git_ref_capability_descriptor_ok "
        f"coordinator={descriptor['coordinator_id']} probe_version={descriptor['source_probe']['probe_version']} "
        "authorized=0 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
