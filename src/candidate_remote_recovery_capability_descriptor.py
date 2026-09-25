#!/usr/bin/env python3
"""Derive a durable-recovery capability descriptor from a passed live rclone probe.

This helper is fail-closed. It accepts only reports emitted by
``candidate_remote_recovery_probe.py`` V0.1.0 or later, requires all recovery
properties currently exercised by that probe, and emits a descriptor suitable
for ``candidate_remote_recovery_policy.py --descriptor``.

It does not contact remote storage, does not write 00_LIBRARY, and does not
authorize publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DESCRIPTOR_VERSION = "0.1.0"
MIN_PROBE_VERSION = (0, 1, 0)
STORAGE_ID = "rclone-private-recovery"
RECOVERY_KIND = "DURABLE_REMOTE_SNAPSHOT_JOURNAL"
FIXTURE_PREFIX = "99_INBOX/CANDIDATES/RECOVERY_REMOTE_FIXTURE/"

REQUIRED_TRUE = {
    "journal_persisted",
    "snapshot_verified",
    "restart_recovery_verified",
    "rollback_verified",
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


def valid_fixture_id(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 24 and all(ch in "0123456789abcdef" for ch in value)


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

    fixture_id = payload.get("fixture_id")
    fixture_path = payload.get("fixture_path")
    if not valid_fixture_id(fixture_id):
        errors.append("probe_fixture_id")
    expected_path = f"{FIXTURE_PREFIX}{fixture_id}" if valid_fixture_id(fixture_id) else None
    if not isinstance(fixture_path, str) or fixture_path != expected_path:
        errors.append("probe_fixture_path")

    if errors:
        return None, sorted(set(errors))

    report_sha = sha256(raw)
    descriptor = {
        "schema_version": SCHEMA_VERSION,
        "descriptor_version": DESCRIPTOR_VERSION,
        "storage_id": STORAGE_ID,
        "recovery_kind": RECOVERY_KIND,
        "evidence": [
            f"live_probe_sha256:{report_sha}",
            f"live_probe_version:{payload['probe_version']}",
            f"disposable_fixture_path:{fixture_path}",
            "live_probe:journal_persisted",
            "live_probe:snapshot_verified",
            "live_probe:restart_recovery_verified",
            "live_probe:rollback_verified",
            "live_probe:cleanup_verified",
        ],
        "guarantees": {
            "durable_journal_persistence": True,
            "snapshot_integrity_verified": True,
            "restart_independent_recovery": True,
            "byte_exact_rollback_verified": True,
            "terminal_recovery_state_verified": True,
            "private_recovery_scope": True,
            "cleanup_verified": True,
        },
        "source_probe": {
            "report_sha256": report_sha,
            "probe_version": payload["probe_version"],
            "fixture_id": fixture_id,
            "fixture_path": fixture_path,
        },
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }
    return descriptor, []


def main() -> int:
    parser = argparse.ArgumentParser(description="Derive a remote recovery capability descriptor from a passed live probe report.")
    parser.add_argument("--probe-report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    try:
        raw = args.probe_report.read_bytes()
    except OSError:
        print("candidate_remote_recovery_capability_descriptor_error code=probe_report_missing canonical_write=0")
        return 2

    descriptor, errors = derive_descriptor(raw)
    if errors or descriptor is None:
        print(
            "candidate_remote_recovery_capability_descriptor_error "
            f"codes={','.join(errors or ['derive_failed'])} canonical_write=0"
        )
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_remote_recovery_capability_descriptor_ok "
        f"storage={descriptor['storage_id']} probe_version={descriptor['source_probe']['probe_version']} "
        "authorized=0 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
