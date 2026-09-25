#!/usr/bin/env python3
"""Replicate critical Technology Library state to a standby rclone backend.

The standby is intentionally not a byte-for-byte archive of all storage. V1
replicates the operational state required to continue after a provider outage
while omitting processed raw media and retention trash to conserve free quota.

Replication is destructive only on the standby side and uses rclone --backup-dir
so replaced/deleted destination files are quarantined instead of permanently
removed during synchronization.
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

from storage_preflight import run_preflight

REPLICA_VERSION = "0.2.0"
SCHEMA_VERSION = 1
DEFAULT_STATE_PATH = "99_INBOX/PROCESS_LOG/STORAGE/replication-state.json"
SAFE_PROVIDER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

PRE_SCOPES = (
    "99_INBOX/DROP_HERE",
    "99_INBOX/TO_REVIEW",
    "99_INBOX/CANDIDATES",
)

POST_SCOPES = (
    "00_LIBRARY",
    "99_INBOX/DROP_HERE",
    "99_INBOX/TO_REVIEW",
    "99_INBOX/CANDIDATES",
    "99_INBOX/READY_FOR_ANALYSIS",
    "99_INBOX/PROCESS_LOG",
    "99_INBOX/CURATION",
    "99_INBOX/ERROR",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *arguments], check=False, capture_output=True, text=True
    )


def slug_scope(scope: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", scope.strip("/"))[:120]


def ensure_scope(remote: str, scope: str) -> bool:
    return run_rclone([
        "mkdir", join_remote(remote, scope), "--log-level", "ERROR"
    ]).returncode == 0


def sync_scope(source: str, destination: str, scope: str, trash_stamp: str) -> bool:
    source_path = join_remote(source, scope)
    destination_path = join_remote(destination, scope)
    if not ensure_scope(source, scope) or not ensure_scope(destination, scope):
        return False

    backup_path = join_remote(
        destination,
        f"99_INBOX/REPLICA_TRASH/{trash_stamp}/{slug_scope(scope)}",
    )
    result = run_rclone([
        "sync",
        source_path,
        destination_path,
        "--backup-dir",
        backup_path,
        "--create-empty-src-dirs",
        "--log-level",
        "ERROR",
        "--stats",
        "0",
    ])
    return result.returncode == 0


def write_replication_state(
    destination: str,
    relative: str,
    *,
    phase: str,
    source_provider: str,
    destination_provider: str,
    scopes: tuple[str, ...],
) -> bool:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "replica_version": REPLICA_VERSION,
        "complete": True,
        "phase": phase,
        "completed_at": utc_now(),
        "source_role": "primary",
        "destination_role": "standby",
        "source_provider": source_provider,
        "destination_provider": destination_provider,
        "scope_count": len(scopes),
        "scope_policy": "critical_state_v1",
    }
    temp = Path("replication-state.json")
    try:
        temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        parent = relative.rsplit("/", 1)[0] if "/" in relative else ""
        if parent and run_rclone(["mkdir", join_remote(destination, parent), "--log-level", "ERROR"]).returncode != 0:
            return False
        return run_rclone([
            "copyto", str(temp), join_remote(destination, relative),
            "--log-level", "ERROR", "--stats", "0",
        ]).returncode == 0
    finally:
        temp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Replicate critical private storage state to standby.")
    parser.add_argument("--source", default="tl:")
    parser.add_argument("--destination", default="tl_backup:")
    parser.add_argument("--source-provider", default="google_drive")
    parser.add_argument("--destination-provider", default="onedrive")
    parser.add_argument("--phase", choices=("pre", "post"), required=True)
    parser.add_argument("--state-path", default=DEFAULT_STATE_PATH)
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("storage_replica_error code=rclone_missing")
        return 2

    source_ok, source_code = run_preflight(args.source)
    destination_ok, destination_code = run_preflight(args.destination)
    if not source_ok:
        print(f"storage_replica_error code=source_{source_code}")
        return 2
    if not destination_ok:
        print(f"storage_replica_error code=destination_{destination_code}")
        return 2

    source_provider = args.source_provider if SAFE_PROVIDER_RE.fullmatch(args.source_provider or "") else "primary"
    destination_provider = args.destination_provider if SAFE_PROVIDER_RE.fullmatch(args.destination_provider or "") else "backup"
    scopes = PRE_SCOPES if args.phase == "pre" else POST_SCOPES
    trash_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    completed = 0
    for scope in scopes:
        if not sync_scope(args.source, args.destination, scope, trash_stamp):
            print(f"storage_replica_error code=scope_sync_failed completed={completed} total={len(scopes)}")
            return 2
        completed += 1

    if not write_replication_state(
        args.destination,
        args.state_path,
        phase=args.phase,
        source_provider=source_provider,
        destination_provider=destination_provider,
        scopes=scopes,
    ):
        print("storage_replica_error code=state_write_failed")
        return 2

    print(f"storage_replica_ok phase={args.phase} scopes={len(scopes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
