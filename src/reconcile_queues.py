#!/usr/bin/env python3
"""Queue-aware reconciliation across Technology Library ingestion pipelines.

This dispatcher reads each process record's original source queue and applies
retry/finalization rules only to the matching processed/error destinations.
That prevents cross-type repairs such as moving audio, image, link, document or
spreadsheet data into a video folder.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from package_contracts import required_package_files
from reconcile import (
    DEFAULT_MAX_RECORDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_READY,
    DEFAULT_RECORDS,
    DEFAULT_RETRY_STATE,
    delete_retry_state,
    ensure_remote_dir,
    exhaust_record,
    join_remote,
    list_json_records,
    load_retry_attempts,
    move_source,
    package_id_from_record,
    read_remote_json,
    record_error,
    record_retryable,
    record_status,
    routing_folder,
    save_retry_state,
    source_folder,
    source_name,
    sync_missing_central_records,
    sync_record,
    update_reconciliation,
    remote_exists,
)

QUEUE_MAP = {
    "99_INBOX/TO_REVIEW/VIDEOS": {
        "processed": "99_INBOX/PROCESSED/VIDEOS",
        "error": "99_INBOX/ERROR/VIDEOS",
    },
    "99_INBOX/TO_REVIEW/IMAGES": {
        "processed": "99_INBOX/PROCESSED/IMAGES",
        "error": "99_INBOX/ERROR/IMAGES",
    },
    "99_INBOX/TO_REVIEW/AUDIO": {
        "processed": "99_INBOX/PROCESSED/AUDIO",
        "error": "99_INBOX/ERROR/AUDIO",
    },
    "99_INBOX/TO_REVIEW/TEXT": {
        "processed": "99_INBOX/PROCESSED/TEXT",
        "error": "99_INBOX/ERROR/TEXT",
    },
    "99_INBOX/TO_REVIEW/DOCUMENTS": {
        "processed": "99_INBOX/PROCESSED/DOCUMENTS",
        "error": "99_INBOX/ERROR/DOCUMENTS",
    },
    "99_INBOX/TO_REVIEW/SPREADSHEETS": {
        "processed": "99_INBOX/PROCESSED/SPREADSHEETS",
        "error": "99_INBOX/ERROR/SPREADSHEETS",
    },
    "99_INBOX/TO_REVIEW/LINKS": {
        "processed": "99_INBOX/PROCESSED/LINKS",
        "error": "99_INBOX/ERROR/LINKS",
    },
}


def normalize_folder(value: str) -> str:
    return value.strip("/")


def route_for_record(record: dict[str, Any]) -> tuple[str, str, str] | None:
    original = normalize_folder(source_folder(record, ""))
    config = QUEUE_MAP.get(original)
    if not config:
        return None
    return original, config["processed"], config["error"]


def package_complete_for_queue(remote_root: str, ready: str, pid: str, source_queue: str) -> bool:
    required = required_package_files(source_queue)
    if not required:
        return False
    base = str(PurePosixPath(ready) / pid)
    return all(
        remote_exists(join_remote(remote_root, str(PurePosixPath(base) / name)))
        for name in required
    )


def reconcile_all(
    *,
    remote_root: str,
    workspace: Path,
    records_folder: str,
    retry_folder: str,
    ready: str,
    max_retries: int,
    max_records: int,
) -> tuple[bool, dict[str, int]]:
    stats = {
        "scanned": 0,
        "synced": 0,
        "requeued": 0,
        "finalized": 0,
        "exhausted": 0,
        "pending": 0,
        "retry_state_cleared": 0,
        "ignored": 0,
        "errors": 0,
    }

    required = {records_folder, retry_folder, ready}
    for original, config in QUEUE_MAP.items():
        required.update({original, config["processed"], config["error"]})
    for folder in required:
        if not ensure_remote_dir(remote_root, folder):
            stats["errors"] += 1
            return False, stats

    synced, sync_errors = sync_missing_central_records(remote_root, ready, records_folder)
    stats["synced"] += synced
    stats["errors"] += sync_errors

    items = list_json_records(remote_root, records_folder)
    if items is None:
        stats["errors"] += 1
        return False, stats

    for item in items[:max_records]:
        path = item.get("Path") or item.get("Name")
        if not isinstance(path, str) or not path.endswith(".json"):
            continue
        record = read_remote_json(join_remote(remote_root, str(PurePosixPath(records_folder) / path)))
        if not record:
            stats["errors"] += 1
            continue

        pid = package_id_from_record(record)
        name = source_name(record)
        route = route_for_record(record)
        if not pid or not name or not route:
            stats["ignored"] += 1
            continue
        original_folder, processed_folder, error_folder = route

        status = record_status(record)
        if status == "processed":
            if load_retry_attempts(remote_root, retry_folder, pid) > 0:
                delete_retry_state(remote_root, retry_folder, pid)
                stats["retry_state_cleared"] += 1
            continue
        if status == "retry_queued":
            stats["pending"] += 1
            continue
        if status != "error" or not record_retryable(record):
            continue

        stats["scanned"] += 1
        attempts = load_retry_attempts(remote_root, retry_folder, pid)
        if attempts >= max_retries:
            if exhaust_record(
                remote_root=remote_root,
                record=record,
                package_id=pid,
                name=name,
                inbox=original_folder,
                error_folder=error_folder,
                records_folder=records_folder,
                ready=ready,
                attempts=attempts,
                max_retries=max_retries,
                workspace=workspace,
            ):
                stats["exhausted"] += 1
            else:
                stats["errors"] += 1
            continue

        code = record_error(record) or "unknown_retryable_error"
        next_attempt = attempts + 1

        if code.startswith("source_move_failed") and package_complete_for_queue(
            remote_root, ready, pid, original_folder
        ):
            if move_source(remote_root, original_folder, processed_folder, name, pid):
                update_reconciliation(
                    record,
                    attempts=next_attempt,
                    max_retries=max_retries,
                    state="resolved_without_reprocessing",
                    status="processed",
                    error_code=None,
                    retryable=False,
                    destination_folder=processed_folder,
                )
                if sync_record(
                    remote_root, records_folder, ready, pid, record, workspace, sync_package=True
                ):
                    delete_retry_state(remote_root, retry_folder, pid)
                    stats["finalized"] += 1
                else:
                    stats["errors"] += 1
                continue

            save_retry_state(remote_root, retry_folder, pid, next_attempt, code, workspace)
            update_reconciliation(
                record,
                attempts=next_attempt,
                max_retries=max_retries,
                state="finalize_move_failed",
            )
            if not sync_record(remote_root, records_folder, ready, pid, record, workspace, sync_package=True):
                stats["errors"] += 1
            continue

        current_folder = normalize_folder(routing_folder(record) or original_folder)
        current_path = join_remote(remote_root, str(PurePosixPath(current_folder) / name))
        original_path = join_remote(remote_root, str(PurePosixPath(original_folder) / name))

        if current_folder != original_folder and remote_exists(current_path):
            if not move_source(remote_root, current_folder, original_folder, name, pid):
                save_retry_state(remote_root, retry_folder, pid, next_attempt, code, workspace)
                update_reconciliation(
                    record,
                    attempts=next_attempt,
                    max_retries=max_retries,
                    state="requeue_move_failed",
                )
                if not sync_record(remote_root, records_folder, ready, pid, record, workspace, sync_package=False):
                    stats["errors"] += 1
                continue
        elif not remote_exists(original_path):
            stats["errors"] += 1
            continue

        if not save_retry_state(remote_root, retry_folder, pid, next_attempt, code, workspace):
            stats["errors"] += 1
            continue
        update_reconciliation(
            record,
            attempts=next_attempt,
            max_retries=max_retries,
            state="queued",
            status="retry_queued",
            retryable=True,
            destination_folder=original_folder,
        )
        if sync_record(remote_root, records_folder, ready, pid, record, workspace, sync_package=False):
            stats["requeued"] += 1
        else:
            stats["errors"] += 1

    return stats["errors"] == 0, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile retryable state across known Technology Library queues.")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--records", default=DEFAULT_RECORDS)
    parser.add_argument("--retry-state", default=DEFAULT_RETRY_STATE)
    parser.add_argument("--ready", default=DEFAULT_READY)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS)
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("reconcile_queues_error code=rclone_missing")
        return 2
    if not 1 <= args.max_retries <= 10:
        parser.error("--max-retries must be between 1 and 10")
    if not 1 <= args.max_records <= 2000:
        parser.error("--max-records must be between 1 and 2000")

    ok, stats = reconcile_all(
        remote_root=args.remote,
        workspace=args.workspace.resolve(),
        records_folder=args.records,
        retry_folder=args.retry_state,
        ready=args.ready,
        max_retries=args.max_retries,
        max_records=args.max_records,
    )
    if not ok:
        print(f"reconcile_queues_error code=reconciliation_failed scanned={stats['scanned']} errors={stats['errors']}")
        return 2

    print(
        "reconcile_queues_ok "
        f"scanned={stats['scanned']} synced={stats['synced']} requeued={stats['requeued']} "
        f"finalized={stats['finalized']} exhausted={stats['exhausted']} pending={stats['pending']} "
        f"cleared={stats['retry_state_cleared']} ignored={stats['ignored']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())