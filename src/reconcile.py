#!/usr/bin/env python3
"""Bounded provider-neutral reconciliation for Technology Library ingestion.

This stage repairs operational state before new work starts. It never performs
semantic analysis and never prints private filenames or record contents.

Supported repairs:
- restore a missing central PROCESS_LOG record from READY_FOR_ANALYSIS
- requeue retryable processing/transport failures with a bounded retry counter
- finalize a source move without reprocessing when the package is already ready
- stop retry loops after a fixed number of automatic retries
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_RECORDS = "99_INBOX/PROCESS_LOG/RECORDS"
DEFAULT_RETRY_STATE = "99_INBOX/PROCESS_LOG/RETRY"
DEFAULT_READY = "99_INBOX/READY_FOR_ANALYSIS"
DEFAULT_INBOX = "99_INBOX/TO_REVIEW/VIDEOS"
DEFAULT_PROCESSED = "99_INBOX/PROCESSED/VIDEOS"
DEFAULT_ERROR = "99_INBOX/ERROR/VIDEOS"
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_RECORDS = 200
PACKAGE_ID_RE = re.compile(r"^[0-9a-f]{20}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def ensure_remote_dir(remote_root: str, relative: str) -> bool:
    return run_rclone(["mkdir", join_remote(remote_root, relative), "--log-level", "ERROR"]).returncode == 0


def remote_exists(remote_path: str) -> bool:
    return run_rclone(["lsjson", remote_path, "--stat", "--log-level", "ERROR"]).returncode == 0


def read_remote_json(remote_path: str) -> dict[str, Any] | None:
    result = run_rclone(["cat", remote_path, "--log-level", "ERROR"])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def write_remote_json(remote_path: str, payload: dict[str, Any], workspace: Path, safe_name: str) -> bool:
    workspace.mkdir(parents=True, exist_ok=True)
    local_path = workspace / safe_name
    local_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result = run_rclone(["copyto", str(local_path), remote_path, "--log-level", "ERROR", "--stats", "0"])
    local_path.unlink(missing_ok=True)
    return result.returncode == 0


def list_json_records(remote_root: str, folder: str) -> list[dict[str, Any]] | None:
    result = run_rclone([
        "lsjson",
        join_remote(remote_root, folder),
        "--files-only",
        "--log-level",
        "ERROR",
    ])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    items = [item for item in payload if isinstance(item, dict)]
    items.sort(key=lambda item: str(item.get("ModTime") or ""), reverse=True)
    return items


def list_ready_process_records(remote_root: str, ready: str) -> list[str] | None:
    result = run_rclone([
        "lsjson",
        join_remote(remote_root, ready),
        "--recursive",
        "--files-only",
        "--log-level",
        "ERROR",
    ])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    output: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        path = item.get("Path") or item.get("Name")
        if isinstance(path, str) and path.endswith("/process-record.json"):
            output.append(path)
    return output


def package_id_from_record(record: dict[str, Any]) -> str | None:
    value = record.get("package_id")
    if isinstance(value, str) and PACKAGE_ID_RE.fullmatch(value):
        return value
    return None


def source_name(record: dict[str, Any]) -> str | None:
    source = record.get("source")
    if not isinstance(source, dict):
        return None
    value = source.get("name")
    if not isinstance(value, str) or not value:
        return None
    return PurePosixPath(value).name


def source_folder(record: dict[str, Any], fallback: str) -> str:
    source = record.get("source")
    if isinstance(source, dict):
        value = source.get("source_folder")
        if isinstance(value, str) and value.strip("/"):
            return value.strip("/")
    return fallback


def routing_folder(record: dict[str, Any]) -> str | None:
    routing = record.get("routing")
    if not isinstance(routing, dict):
        return None
    value = routing.get("destination_folder")
    return value.strip("/") if isinstance(value, str) and value.strip("/") else None


def record_status(record: dict[str, Any]) -> str | None:
    processing = record.get("processing")
    if not isinstance(processing, dict):
        return None
    value = processing.get("status")
    return str(value) if value is not None else None


def record_error(record: dict[str, Any]) -> str | None:
    processing = record.get("processing")
    if not isinstance(processing, dict):
        return None
    value = processing.get("error_code")
    return str(value) if value else None


def record_retryable(record: dict[str, Any]) -> bool:
    processing = record.get("processing")
    return bool(processing.get("retryable")) if isinstance(processing, dict) else False


def retry_state_path(remote_root: str, retry_folder: str, package_id: str) -> str:
    return join_remote(remote_root, str(PurePosixPath(retry_folder) / f"{package_id}.json"))


def load_retry_attempts(remote_root: str, retry_folder: str, package_id: str) -> int:
    payload = read_remote_json(retry_state_path(remote_root, retry_folder, package_id))
    if not payload:
        return 0
    try:
        return max(0, int(payload.get("attempts") or 0))
    except (TypeError, ValueError):
        return 0


def save_retry_state(
    remote_root: str,
    retry_folder: str,
    package_id: str,
    attempts: int,
    error_code: str | None,
    workspace: Path,
) -> bool:
    payload = {
        "schema_version": 1,
        "package_id": package_id,
        "attempts": attempts,
        "last_error_code": error_code,
        "updated_at": utc_now(),
    }
    return write_remote_json(
        retry_state_path(remote_root, retry_folder, package_id),
        payload,
        workspace,
        f"retry-{package_id}.json",
    )


def delete_retry_state(remote_root: str, retry_folder: str, package_id: str) -> None:
    run_rclone([
        "deletefile",
        retry_state_path(remote_root, retry_folder, package_id),
        "--log-level",
        "ERROR",
    ])


def update_reconciliation(
    record: dict[str, Any],
    *,
    attempts: int,
    max_retries: int,
    state: str,
    status: str | None = None,
    error_code: str | None | object = ...,
    retryable: bool | None = None,
    destination_folder: str | None = None,
) -> dict[str, Any]:
    processing = record.setdefault("processing", {})
    routing = record.setdefault("routing", {})
    record["reconciliation"] = {
        "attempts": attempts,
        "max_retries": max_retries,
        "state": state,
        "updated_at": utc_now(),
    }
    if status is not None:
        processing["status"] = status
    if error_code is not ...:
        processing["error_code"] = error_code
    if retryable is not None:
        processing["retryable"] = bool(retryable)
    if destination_folder is not None:
        routing["destination_folder"] = destination_folder
    return record


def safe_destination(remote_root: str, folder: str, name: str, package_id: str) -> str:
    normal = join_remote(remote_root, str(PurePosixPath(folder) / name))
    if not remote_exists(normal):
        return normal
    path = PurePosixPath(name)
    stem = path.stem or "source"
    suffix = path.suffix
    alternate = f"{stem}.{package_id}{suffix}"
    return join_remote(remote_root, str(PurePosixPath(folder) / alternate))


def move_source(
    remote_root: str,
    source_folder_value: str,
    destination_folder: str,
    name: str,
    package_id: str,
) -> bool:
    source = join_remote(remote_root, str(PurePosixPath(source_folder_value) / name))
    if not remote_exists(source):
        return False
    destination = safe_destination(remote_root, destination_folder, name, package_id)
    result = run_rclone(["moveto", source, destination, "--log-level", "ERROR", "--stats", "0"])
    return result.returncode == 0


def package_is_complete(remote_root: str, ready: str, package_id: str) -> bool:
    base = str(PurePosixPath(ready) / package_id)
    required = ("ingest.json", "checkpoint.json", "evidence-summary.json", "process-record.json")
    return all(remote_exists(join_remote(remote_root, str(PurePosixPath(base) / name))) for name in required)


def sync_record(
    remote_root: str,
    records_folder: str,
    ready: str,
    package_id: str,
    record: dict[str, Any],
    workspace: Path,
    sync_package: bool,
) -> bool:
    central = join_remote(remote_root, str(PurePosixPath(records_folder) / f"{package_id}.json"))
    if not write_remote_json(central, record, workspace, f"record-{package_id}.json"):
        return False
    if sync_package:
        package_record = join_remote(
            remote_root,
            str(PurePosixPath(ready) / package_id / "process-record.json"),
        )
        if not write_remote_json(package_record, record, workspace, f"package-record-{package_id}.json"):
            return False
    return True


def sync_missing_central_records(
    remote_root: str,
    ready: str,
    records_folder: str,
) -> tuple[int, int]:
    paths = list_ready_process_records(remote_root, ready)
    if paths is None:
        return 0, 1
    synced = 0
    errors = 0
    for relative in paths:
        package_id = PurePosixPath(relative).parent.name
        if not PACKAGE_ID_RE.fullmatch(package_id):
            continue
        central = join_remote(remote_root, str(PurePosixPath(records_folder) / f"{package_id}.json"))
        if remote_exists(central):
            continue
        source = join_remote(remote_root, str(PurePosixPath(ready) / relative))
        result = run_rclone(["copyto", source, central, "--log-level", "ERROR", "--stats", "0"])
        if result.returncode == 0:
            synced += 1
        else:
            errors += 1
    return synced, errors


def exhaust_record(
    *,
    remote_root: str,
    record: dict[str, Any],
    package_id: str,
    name: str,
    inbox: str,
    error_folder: str,
    records_folder: str,
    ready: str,
    attempts: int,
    max_retries: int,
    workspace: Path,
) -> bool:
    current_folder = routing_folder(record) or source_folder(record, inbox)
    if current_folder != error_folder:
        source_path = join_remote(remote_root, str(PurePosixPath(current_folder) / name))
        if remote_exists(source_path):
            move_source(remote_root, current_folder, error_folder, name, package_id)
    update_reconciliation(
        record,
        attempts=attempts,
        max_retries=max_retries,
        state="exhausted",
        status="error",
        retryable=False,
        destination_folder=error_folder,
    )
    return sync_record(
        remote_root,
        records_folder,
        ready,
        package_id,
        record,
        workspace,
        sync_package=package_is_complete(remote_root, ready, package_id),
    )


def reconcile(
    *,
    remote_root: str,
    workspace: Path,
    records_folder: str,
    retry_folder: str,
    ready: str,
    inbox: str,
    processed: str,
    error_folder: str,
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
        "errors": 0,
    }

    for folder in (records_folder, retry_folder, ready, inbox, processed, error_folder):
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
        package_id = package_id_from_record(record)
        name = source_name(record)
        if not package_id or not name:
            continue

        status = record_status(record)
        if status == "retry_queued":
            stats["pending"] += 1
            continue
        if status != "error" or not record_retryable(record):
            continue

        stats["scanned"] += 1
        attempts = load_retry_attempts(remote_root, retry_folder, package_id)
        if attempts >= max_retries:
            if exhaust_record(
                remote_root=remote_root,
                record=record,
                package_id=package_id,
                name=name,
                inbox=inbox,
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

        if code.startswith("source_move_failed") and package_is_complete(remote_root, ready, package_id):
            original_folder = source_folder(record, inbox)
            if move_source(remote_root, original_folder, processed, name, package_id):
                update_reconciliation(
                    record,
                    attempts=next_attempt,
                    max_retries=max_retries,
                    state="resolved_without_reprocessing",
                    status="processed",
                    error_code=None,
                    retryable=False,
                    destination_folder=processed,
                )
                if sync_record(
                    remote_root,
                    records_folder,
                    ready,
                    package_id,
                    record,
                    workspace,
                    sync_package=True,
                ):
                    delete_retry_state(remote_root, retry_folder, package_id)
                    stats["finalized"] += 1
                else:
                    stats["errors"] += 1
                continue

            save_retry_state(remote_root, retry_folder, package_id, next_attempt, code, workspace)
            update_reconciliation(
                record,
                attempts=next_attempt,
                max_retries=max_retries,
                state="finalize_move_failed",
            )
            if not sync_record(remote_root, records_folder, ready, package_id, record, workspace, sync_package=True):
                stats["errors"] += 1
            continue

        current_folder = routing_folder(record) or source_folder(record, inbox)
        original_folder = source_folder(record, inbox)
        source_current = join_remote(remote_root, str(PurePosixPath(current_folder) / name))
        source_original = join_remote(remote_root, str(PurePosixPath(original_folder) / name))

        if current_folder != original_folder and remote_exists(source_current):
            if not move_source(remote_root, current_folder, original_folder, name, package_id):
                save_retry_state(remote_root, retry_folder, package_id, next_attempt, code, workspace)
                update_reconciliation(
                    record,
                    attempts=next_attempt,
                    max_retries=max_retries,
                    state="requeue_move_failed",
                )
                if not sync_record(remote_root, records_folder, ready, package_id, record, workspace, sync_package=False):
                    stats["errors"] += 1
                continue
        elif not remote_exists(source_original):
            stats["errors"] += 1
            continue

        if not save_retry_state(remote_root, retry_folder, package_id, next_attempt, code, workspace):
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
        if sync_record(remote_root, records_folder, ready, package_id, record, workspace, sync_package=False):
            stats["requeued"] += 1
        else:
            stats["errors"] += 1

    return stats["errors"] == 0, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile retryable Technology Library ingestion state.")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--records", default=DEFAULT_RECORDS)
    parser.add_argument("--retry-state", default=DEFAULT_RETRY_STATE)
    parser.add_argument("--ready", default=DEFAULT_READY)
    parser.add_argument("--inbox", default=DEFAULT_INBOX)
    parser.add_argument("--processed", default=DEFAULT_PROCESSED)
    parser.add_argument("--error", default=DEFAULT_ERROR)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS)
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("reconcile_error code=rclone_missing")
        return 2
    if not 1 <= args.max_retries <= 10:
        parser.error("--max-retries must be between 1 and 10")
    if not 1 <= args.max_records <= 2000:
        parser.error("--max-records must be between 1 and 2000")

    ok, stats = reconcile(
        remote_root=args.remote,
        workspace=args.workspace.resolve(),
        records_folder=args.records,
        retry_folder=args.retry_state,
        ready=args.ready,
        inbox=args.inbox,
        processed=args.processed,
        error_folder=args.error,
        max_retries=args.max_retries,
        max_records=args.max_records,
    )
    if not ok:
        print(
            "reconcile_error code=reconciliation_failed "
            f"scanned={stats['scanned']} errors={stats['errors']}"
        )
        return 2

    print(
        "reconcile_ok "
        f"scanned={stats['scanned']} "
        f"synced={stats['synced']} "
        f"requeued={stats['requeued']} "
        f"finalized={stats['finalized']} "
        f"exhausted={stats['exhausted']} "
        f"pending={stats['pending']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
