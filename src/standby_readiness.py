#!/usr/bin/env python3
"""Read-only readiness check for a replicated standby storage backend."""

from __future__ import annotations

import argparse

from storage_failover import (
    DEFAULT_FAILOVER_LOCK,
    DEFAULT_REPLICATION_STATE,
    failover_lock_active,
    join_remote,
    remote_json,
    replication_is_fresh,
)
from storage_preflight import run_preflight

READINESS_VERSION = "0.1.0"
REQUIRED_ROOTS = ("00_LIBRARY", "99_INBOX")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check standby readiness without writing to storage.")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--max-backup-age-hours", type=int, default=24)
    parser.add_argument("--replication-state", default=DEFAULT_REPLICATION_STATE)
    parser.add_argument("--failover-lock", default=DEFAULT_FAILOVER_LOCK)
    parser.add_argument("--require-unlocked", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.max_backup_age_hours <= 168:
        parser.error("--max-backup-age-hours must be between 1 and 168")

    ok, code = run_preflight(args.remote)
    if not ok:
        print(f"standby_readiness_error code={code}")
        return 2

    for relative in REQUIRED_ROOTS:
        root_ok, _ = run_preflight(join_remote(args.remote, relative))
        if not root_ok:
            print("standby_readiness_error code=critical_root_missing")
            return 2

    replication = remote_json(args.remote, args.replication_state)
    if not replication_is_fresh(replication, args.max_backup_age_hours):
        print("standby_readiness_error code=full_post_not_fresh")
        return 2

    lock = remote_json(args.remote, args.failover_lock)
    locked = failover_lock_active(lock)
    if args.require_unlocked and locked:
        print("standby_readiness_error code=failover_lock_active")
        return 2

    print(
        "standby_readiness_ok "
        f"version={READINESS_VERSION} roots={len(REQUIRED_ROOTS)} full_post=1 lock_active={1 if locked else 0}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
