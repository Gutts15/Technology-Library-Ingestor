#!/usr/bin/env python3
"""Report aggregate generic artifacts blocking legacy READY metadata backfill.

This helper deliberately prints only current contract artifact names and counts.
It never prints package IDs, source names, provider IDs or evidence content.
"""

from __future__ import annotations

import argparse
import shutil
from collections import Counter

from legacy_ready_backfill import (
    join_remote,
    list_remote_packages,
    package_audit,
    remote_entries,
    remote_json,
    remote_text,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate missing artifact reasons for legacy READY packages.")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--source", default="99_INBOX/READY_FOR_ANALYSIS")
    parser.add_argument("--max-packages", type=int, default=100)
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("legacy_ready_missing_error code=rclone_missing")
        return 2

    package_ids = list_remote_packages(args.remote, args.source, args.max_packages)
    if package_ids is None:
        print("legacy_ready_missing_error code=package_list_failed")
        return 2

    missing = Counter()
    blocked = 0
    backfillable = 0
    current = 0
    for package_id in package_ids:
        package_remote = join_remote(args.remote, f"{args.source.strip('/')}/{package_id}")
        entries = remote_entries(package_remote)
        if entries is None:
            missing["package_list_failed"] += 1
            blocked += 1
            continue
        filenames = {
            str(item.get("Name"))
            for item in entries
            if not item.get("IsDir") and isinstance(item.get("Name"), str)
        }
        process_text = remote_text(f"{package_remote}/process-record.json")
        ingest = remote_json(f"{package_remote}/ingest.json")
        audit = package_audit(
            package_id=package_id,
            filenames=filenames,
            process_text=process_text,
            ingest=ingest,
        )
        state = audit.get("state")
        if state == "current":
            current += 1
        elif state == "backfillable":
            backfillable += 1
        else:
            blocked += 1
            values = audit.get("missing") or ()
            if values:
                missing.update(str(value) for value in values)
            else:
                missing[str(audit.get("reason") or "unknown")] += 1

    missing_text = ",".join(f"{key}:{missing[key]}" for key in sorted(missing)) or "none"
    print(
        "legacy_ready_missing_ok "
        f"seen={len(package_ids)} current={current} backfillable={backfillable} blocked={blocked} "
        f"missing={missing_text}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
