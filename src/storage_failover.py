#!/usr/bin/env python3
"""Select a private storage backend without leaking provider details.

Primary storage is preferred during normal operation. A configured backup may
be selected only when:
- primary preflight fails;
- backup preflight succeeds;
- backup carries a recent successful full post-run replication marker.

Once failover is activated, a small lock is written to the backup. While that
lock exists, the selector keeps using the backup even if the primary later
returns. This prevents automatic failback from creating split-brain state.
Failback is intentionally a separate reconciliation operation.
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

from storage_preflight import run_preflight

SELECTOR_VERSION = "0.2.0"
SCHEMA_VERSION = 1
DEFAULT_REPLICATION_STATE = "99_INBOX/PROCESS_LOG/STORAGE/replication-state.json"
DEFAULT_FAILOVER_LOCK = "99_INBOX/PROCESS_LOG/STORAGE/failover-active.json"
SAFE_PROVIDER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
REQUIRED_REPLICATION_PHASE = "post"
REQUIRED_SCOPE_POLICY = "critical_state_v1"
REQUIRED_SCOPE_COUNT = 8


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def safe_provider(value: str, fallback: str) -> str:
    return value if SAFE_PROVIDER_RE.fullmatch(value or "") else fallback


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *arguments], check=False, capture_output=True, text=True
    )


def remote_json(remote: str, relative: str) -> dict[str, Any] | None:
    if not shutil.which("rclone"):
        return None
    result = run_rclone(["cat", join_remote(remote, relative), "--log-level", "ERROR"])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def replication_is_fresh(payload: dict[str, Any] | None, max_age_hours: int) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("complete") is not True:
        return False
    if payload.get("phase") != REQUIRED_REPLICATION_PHASE:
        return False
    if payload.get("scope_policy") != REQUIRED_SCOPE_POLICY:
        return False
    if payload.get("scope_count") != REQUIRED_SCOPE_COUNT:
        return False
    completed = parse_iso(payload.get("completed_at"))
    if completed is None:
        return False
    age_seconds = (datetime.now(timezone.utc) - completed).total_seconds()
    return 0 <= age_seconds <= max_age_hours * 3600


def failover_lock_active(payload: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(payload, dict)
        and payload.get("schema_version") == SCHEMA_VERSION
        and payload.get("active") is True
    )


def write_failover_lock(remote: str, relative: str) -> bool:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "selector_version": SELECTOR_VERSION,
        "active": True,
        "activated_at": utc_now(),
        "reason": "primary_unavailable",
    }
    temp = Path("failover-active.json")
    try:
        temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        parent = relative.rsplit("/", 1)[0] if "/" in relative else ""
        if parent and run_rclone(["mkdir", join_remote(remote, parent), "--log-level", "ERROR"]).returncode != 0:
            return False
        return run_rclone([
            "copyto", str(temp), join_remote(remote, relative),
            "--log-level", "ERROR", "--stats", "0",
        ]).returncode == 0
    finally:
        temp.unlink(missing_ok=True)


def write_env(path: Path, *, remote: str, provider: str, mode: str, backup_configured: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join([
            f"TL_ACTIVE_REMOTE={remote}",
            f"TL_EFFECTIVE_PROVIDER={provider}",
            f"TL_STORAGE_MODE={mode}",
            f"TL_BACKUP_CONFIGURED={1 if backup_configured else 0}",
        ]) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Select primary or replicated standby storage safely.")
    parser.add_argument("--primary", default="tl:")
    parser.add_argument("--backup", default="tl_backup:")
    parser.add_argument("--primary-provider", default="google_drive")
    parser.add_argument("--backup-provider", default="onedrive")
    parser.add_argument("--backup-configured", action="store_true")
    parser.add_argument("--max-backup-age-hours", type=int, default=24)
    parser.add_argument("--replication-state", default=DEFAULT_REPLICATION_STATE)
    parser.add_argument("--failover-lock", default=DEFAULT_FAILOVER_LOCK)
    parser.add_argument("--env-out", type=Path, required=True)
    args = parser.parse_args()

    if not 1 <= args.max_backup_age_hours <= 168:
        parser.error("--max-backup-age-hours must be between 1 and 168")

    primary_provider = safe_provider(args.primary_provider, "primary")
    backup_provider = safe_provider(args.backup_provider, "backup")
    primary_ok, primary_code = run_preflight(args.primary)

    if not args.backup_configured:
        if not primary_ok:
            print(f"storage_select_error code={primary_code} backup=unconfigured")
            return 2
        write_env(args.env_out, remote=args.primary, provider=primary_provider, mode="primary", backup_configured=False)
        print("storage_select_ok mode=primary backup=unconfigured")
        return 0

    backup_ok, backup_code = run_preflight(args.backup)
    lock = remote_json(args.backup, args.failover_lock) if backup_ok else None

    if failover_lock_active(lock):
        if not backup_ok:
            print(f"storage_select_error code={backup_code} failover_lock=active")
            return 2
        write_env(args.env_out, remote=args.backup, provider=backup_provider, mode="fallback_locked", backup_configured=True)
        print("storage_select_ok mode=fallback_locked")
        return 0

    if primary_ok:
        write_env(args.env_out, remote=args.primary, provider=primary_provider, mode="primary", backup_configured=True)
        print(f"storage_select_ok mode=primary backup_ready={1 if backup_ok else 0}")
        return 0

    if not backup_ok:
        print(f"storage_select_error code=primary_{primary_code}_backup_{backup_code}")
        return 2

    replication = remote_json(args.backup, args.replication_state)
    if not replication_is_fresh(replication, args.max_backup_age_hours):
        print("storage_select_error code=backup_not_fresh")
        return 2

    if not write_failover_lock(args.backup, args.failover_lock):
        print("storage_select_error code=failover_lock_write_failed")
        return 2

    write_env(args.env_out, remote=args.backup, provider=backup_provider, mode="fallback", backup_configured=True)
    print("storage_select_ok mode=fallback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
