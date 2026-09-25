#!/usr/bin/env python3
"""Safely reconcile an active standby back into a recovered primary.

The standby is authoritative while a failover lock is active. Reconciliation
copies the configured critical scopes from standby to primary, quarantines any
primary-side displaced data, validates equality, refreshes the standby marker,
and removes the failover lock only after validation succeeds.

The operation is fail-closed: any error before the backup lock is removed leaves
that lock active so normal ingestion continues on the standby. No filenames or
file contents are emitted to stdout/stderr by this script.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from storage_failover import (
    DEFAULT_FAILOVER_LOCK,
    DEFAULT_REPLICATION_STATE,
    REQUIRED_REPLICATION_PHASE,
    REQUIRED_SCOPE_COUNT,
    REQUIRED_SCOPE_POLICY,
    failover_lock_active,
    remote_json,
    safe_provider,
)
from storage_preflight import run_preflight
from storage_replica import POST_SCOPES, write_replication_state

RECONCILE_VERSION = "0.1.0"
SCHEMA_VERSION = 1
DEFAULT_RECONCILE_STATE = "99_INBOX/PROCESS_LOG/STORAGE/reconciliation-state.json"
DEFAULT_QUARANTINE_ROOT = "99_INBOX/FAILBACK_QUARANTINE"
DEEP_VALIDATE_SCOPES = {
    "99_INBOX/PROCESS_LOG",
    "99_INBOX/CURATION",
}
SAFE_RELATIVE_RE = re.compile(r"^[A-Za-z0-9._/ -]{1,240}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *arguments], check=False, capture_output=True, text=True
    )


def slug_scope(scope: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", scope.strip("/"))[:120]


def safe_relative(value: str) -> bool:
    if not SAFE_RELATIVE_RE.fullmatch(value or ""):
        return False
    parts = [part for part in value.replace("\\", "/").split("/") if part]
    return bool(parts) and all(part not in {".", ".."} for part in parts)


def ensure_scope(remote: str, scope: str) -> bool:
    result = run_rclone([
        "mkdir", join_remote(remote, scope), "--log-level", "ERROR"
    ])
    return result.returncode == 0


def write_json_remote(remote: str, relative: str, payload: dict[str, Any]) -> bool:
    temp = Path("storage-reconcile-state.json")
    try:
        temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        parent = relative.rsplit("/", 1)[0] if "/" in relative else ""
        if parent and not ensure_scope(remote, parent):
            return False
        result = run_rclone([
            "copyto", str(temp), join_remote(remote, relative),
            "--log-level", "ERROR", "--stats", "0",
        ])
        return result.returncode == 0
    finally:
        temp.unlink(missing_ok=True)


def copy_remote_file(source: str, destination: str, relative: str) -> bool:
    result = run_rclone([
        "copyto", join_remote(source, relative), join_remote(destination, relative),
        "--log-level", "ERROR", "--stats", "0",
    ])
    return result.returncode == 0


def remote_file_exists(remote: str, relative: str) -> bool:
    result = run_rclone([
        "cat", join_remote(remote, relative), "--log-level", "ERROR"
    ])
    return result.returncode == 0


def delete_remote_file_if_exists(remote: str, relative: str) -> bool:
    if not remote_file_exists(remote, relative):
        return True
    result = run_rclone([
        "deletefile", join_remote(remote, relative), "--log-level", "ERROR"
    ])
    return result.returncode == 0


def marker_is_complete(payload: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(payload, dict)
        and payload.get("schema_version") == SCHEMA_VERSION
        and payload.get("complete") is True
        and payload.get("phase") == REQUIRED_REPLICATION_PHASE
        and payload.get("scope_policy") == REQUIRED_SCOPE_POLICY
        and payload.get("scope_count") == REQUIRED_SCOPE_COUNT
    )


def sync_scope(
    source: str,
    destination: str,
    scope: str,
    quarantine_root: str,
    stamp: str,
) -> bool:
    if not ensure_scope(source, scope) or not ensure_scope(destination, scope):
        return False
    quarantine = join_remote(
        destination,
        f"{quarantine_root.strip('/')}/{stamp}/{slug_scope(scope)}",
    )
    result = run_rclone([
        "sync",
        join_remote(source, scope),
        join_remote(destination, scope),
        "--backup-dir", quarantine,
        "--create-empty-src-dirs",
        "--log-level", "ERROR",
        "--stats", "0",
    ])
    return result.returncode == 0


def check_scope(source: str, destination: str, scope: str, *, deep: bool) -> bool:
    args = [
        "check",
        join_remote(source, scope),
        join_remote(destination, scope),
    ]
    if deep:
        args.append("--download")
    else:
        args.append("--size-only")
    args.extend(["--log-level", "ERROR", "--stats", "0"])
    result = run_rclone(args)
    return result.returncode == 0


def validate_scopes(
    source: str,
    destination: str,
    scopes: tuple[str, ...],
    deep_scopes: set[str],
) -> tuple[bool, int]:
    completed = 0
    for scope in scopes:
        if not check_scope(source, destination, scope, deep=scope in deep_scopes):
            return False, completed
        completed += 1
    return True, completed


def reconcile_payload(
    status: str,
    *,
    started_at: str,
    primary_provider: str,
    backup_provider: str,
    scopes: tuple[str, ...],
    quarantine_root: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "reconcile_version": RECONCILE_VERSION,
        "status": status,
        "started_at": started_at,
        "source_role": "standby",
        "destination_role": "primary",
        "source_provider": backup_provider,
        "destination_provider": primary_provider,
        "scope_count": len(scopes),
        "scope_policy": "critical_state_v1" if scopes == POST_SCOPES else "custom",
        "quarantine_policy": "preserve_displaced_primary_v1",
        "quarantine_root": quarantine_root,
    }
    if status == "complete":
        payload["completed_at"] = utc_now()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile active standby state into a recovered primary safely."
    )
    parser.add_argument("--primary", default="tl:")
    parser.add_argument("--backup", default="tl_backup:")
    parser.add_argument("--primary-provider", default="google_drive")
    parser.add_argument("--backup-provider", default="onedrive")
    parser.add_argument("--failover-lock", default=DEFAULT_FAILOVER_LOCK)
    parser.add_argument("--replication-state", default=DEFAULT_REPLICATION_STATE)
    parser.add_argument("--reconcile-state", default=DEFAULT_RECONCILE_STATE)
    parser.add_argument("--quarantine-root", default=DEFAULT_QUARANTINE_ROOT)
    parser.add_argument(
        "--scope", action="append", default=[],
        help="Override default critical scopes; repeat for multiple scopes.",
    )
    parser.add_argument(
        "--deep-scope", action="append", default=[],
        help="Scope to validate by downloading content rather than size-only.",
    )
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--skip-replication-marker", action="store_true")
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("storage_reconcile_error code=rclone_missing")
        return 2

    paths_to_validate = [
        args.failover_lock,
        args.replication_state,
        args.reconcile_state,
        args.quarantine_root,
        *args.scope,
        *args.deep_scope,
    ]
    if not all(safe_relative(item) for item in paths_to_validate if item):
        print("storage_reconcile_error code=invalid_relative_path")
        return 2

    scopes = tuple(args.scope) if args.scope else POST_SCOPES
    if len(scopes) > 32 or len(set(scopes)) != len(scopes):
        print("storage_reconcile_error code=invalid_scope_set")
        return 2

    deep_scopes = set(args.deep_scope) if args.deep_scope else (DEEP_VALIDATE_SCOPES & set(scopes))
    if not deep_scopes.issubset(set(scopes)):
        print("storage_reconcile_error code=deep_scope_not_selected")
        return 2

    primary_provider = safe_provider(args.primary_provider, "primary")
    backup_provider = safe_provider(args.backup_provider, "backup")

    backup_ok, backup_code = run_preflight(args.backup)
    if not backup_ok:
        print(f"storage_reconcile_error code=backup_{backup_code}")
        return 2

    lock_payload = remote_json(args.backup, args.failover_lock)
    if not failover_lock_active(lock_payload):
        if args.auto:
            print("storage_reconcile_skip code=no_active_failover")
            return 0
        print("storage_reconcile_error code=failover_lock_missing")
        return 2

    primary_ok, primary_code = run_preflight(args.primary)
    if not primary_ok:
        if args.auto:
            print(f"storage_reconcile_skip code=primary_{primary_code}")
            return 0
        print(f"storage_reconcile_error code=primary_{primary_code}")
        return 2

    use_standard_marker = scopes == POST_SCOPES and not args.skip_replication_marker
    if use_standard_marker:
        marker = remote_json(args.backup, args.replication_state)
        if not marker_is_complete(marker):
            print("storage_reconcile_error code=baseline_replication_invalid")
            return 2

    started_at = utc_now()
    running = reconcile_payload(
        "running",
        started_at=started_at,
        primary_provider=primary_provider,
        backup_provider=backup_provider,
        scopes=scopes,
        quarantine_root=args.quarantine_root,
    )
    if not write_json_remote(args.backup, args.reconcile_state, running):
        print("storage_reconcile_error code=state_start_write_failed")
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    completed = 0
    for scope in scopes:
        if not sync_scope(args.backup, args.primary, scope, args.quarantine_root, stamp):
            print(f"storage_reconcile_error code=scope_sync_failed completed={completed} total={len(scopes)}")
            return 2
        completed += 1

    valid, checked = validate_scopes(args.backup, args.primary, scopes, deep_scopes)
    if not valid:
        print(f"storage_reconcile_error code=validation_failed completed={checked} total={len(scopes)}")
        return 2

    validated = reconcile_payload(
        "validated",
        started_at=started_at,
        primary_provider=primary_provider,
        backup_provider=backup_provider,
        scopes=scopes,
        quarantine_root=args.quarantine_root,
    )
    if not write_json_remote(args.backup, args.reconcile_state, validated):
        print("storage_reconcile_error code=state_validated_write_failed")
        return 2
    if not copy_remote_file(args.backup, args.primary, args.reconcile_state):
        print("storage_reconcile_error code=state_validated_copy_failed")
        return 2

    if use_standard_marker:
        if not write_replication_state(
            args.backup,
            args.replication_state,
            phase="post",
            source_provider=primary_provider,
            destination_provider=backup_provider,
            scopes=POST_SCOPES,
        ):
            print("storage_reconcile_error code=replication_marker_refresh_failed")
            return 2
        if not copy_remote_file(args.backup, args.primary, args.replication_state):
            print("storage_reconcile_error code=replication_marker_copy_failed")
            return 2

    valid, checked = validate_scopes(args.backup, args.primary, scopes, deep_scopes)
    if not valid:
        print(f"storage_reconcile_error code=pre_unlock_validation_failed completed={checked} total={len(scopes)}")
        return 2

    # The primary copy of the lock is removed first. The backup lock is the
    # selector's authoritative lock and is therefore the final commit point.
    if not delete_remote_file_if_exists(args.primary, args.failover_lock):
        print("storage_reconcile_error code=primary_lock_delete_failed")
        return 2
    if not delete_remote_file_if_exists(args.backup, args.failover_lock):
        print("storage_reconcile_error code=backup_lock_delete_failed")
        return 2

    valid, checked = validate_scopes(args.backup, args.primary, scopes, deep_scopes)
    if not valid:
        # Re-arm fallback using the original lock payload if post-unlock
        # validation unexpectedly fails. The recovered primary remains
        # quarantined and can be retried later.
        if isinstance(lock_payload, dict):
            write_json_remote(args.backup, args.failover_lock, lock_payload)
        print(f"storage_reconcile_error code=post_unlock_validation_failed completed={checked} total={len(scopes)}")
        return 2

    complete = reconcile_payload(
        "complete",
        started_at=started_at,
        primary_provider=primary_provider,
        backup_provider=backup_provider,
        scopes=scopes,
        quarantine_root=args.quarantine_root,
    )
    if not write_json_remote(args.backup, args.reconcile_state, complete):
        if isinstance(lock_payload, dict):
            write_json_remote(args.backup, args.failover_lock, lock_payload)
        print("storage_reconcile_error code=state_complete_write_failed")
        return 2
    if not copy_remote_file(args.backup, args.primary, args.reconcile_state):
        if isinstance(lock_payload, dict):
            write_json_remote(args.backup, args.failover_lock, lock_payload)
        print("storage_reconcile_error code=state_complete_copy_failed")
        return 2

    state_scope = next(
        (scope for scope in scopes if args.reconcile_state.startswith(scope.rstrip("/") + "/")),
        None,
    )
    if state_scope and not check_scope(
        args.backup, args.primary, state_scope, deep=state_scope in deep_scopes
    ):
        if isinstance(lock_payload, dict):
            write_json_remote(args.backup, args.failover_lock, lock_payload)
        print("storage_reconcile_error code=final_state_validation_failed")
        return 2

    print(
        "storage_reconcile_ok "
        f"version={RECONCILE_VERSION} scopes={len(scopes)} deep={len(deep_scopes)} quarantine=1"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
