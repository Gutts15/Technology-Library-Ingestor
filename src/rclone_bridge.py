#!/usr/bin/env python3
"""Rclone bridge between a private media inbox and the local batch ingestor.

The same code can target Google Drive, OneDrive, Box, local storage, or any
other rclone backend. Storage credentials are captured by this trusted bridge
and removed from the inherited environment before OCR/transcription subprocesses
are launched.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from evidence_compactor import COMPACTOR_VERSION, build_evidence_summary
from ingest_video import PIPELINE_VERSION
from process_record import build_process_record, write_process_record

DEFAULT_INBOX = "99_INBOX/TO_REVIEW/VIDEOS"
DEFAULT_READY = "99_INBOX/READY_FOR_ANALYSIS"
DEFAULT_PROCESSED = "99_INBOX/PROCESSED/VIDEOS"
DEFAULT_ERROR = "99_INBOX/ERROR/VIDEOS"
DEFAULT_PROCESS_LOG = "99_INBOX/PROCESS_LOG/RECORDS"
DEFAULT_BATCH_SIZE = 5
SUPPORTED_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
VIDEO_MIME_SUFFIXES = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
    "video/avi": ".avi",
    "video/x-m4v": ".m4v",
    "video/m4v": ".m4v",
}
_RCLONE_ENV: dict[str, str] = {}


def capture_rclone_environment() -> None:
    """Remove rclone credentials/config from child-process inheritance."""
    for key in list(os.environ):
        if key == "RCLONE_CONFIG" or key.startswith("RCLONE_CONFIG_") or key.startswith("RCLONE_DRIVE_"):
            _RCLONE_ENV[key] = os.environ.pop(key)


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(_RCLONE_ENV)
    return subprocess.run(
        ["rclone", *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def ensure_remote_dir(remote_root: str, relative: str) -> bool:
    result = run_rclone(["mkdir", join_remote(remote_root, relative), "--log-level", "ERROR"])
    return result.returncode == 0


def video_suffix(item: dict[str, Any]) -> str | None:
    name = item.get("Name") or item.get("Path")
    if isinstance(name, str):
        suffix = Path(name).suffix.lower()
        if suffix in SUPPORTED_SUFFIXES:
            return suffix
    mime_type = item.get("MimeType")
    if isinstance(mime_type, str):
        return VIDEO_MIME_SUFFIXES.get(mime_type.lower().strip())
    return None


def is_supported_video(item: dict[str, Any]) -> bool:
    return video_suffix(item) is not None


def list_pending(remote_root: str, inbox: str) -> list[dict[str, Any]]:
    result = run_rclone(
        [
            "lsjson",
            join_remote(remote_root, inbox),
            "--files-only",
            "--log-level",
            "ERROR",
        ]
    )
    if result.returncode != 0:
        return []
    try:
        items = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []

    pending: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("Name") or item.get("Path")
        if not isinstance(name, str) or not is_supported_video(item):
            continue
        pending.append(item)

    pending.sort(key=lambda item: (str(item.get("ModTime") or ""), str(item.get("Path") or item.get("Name") or "")))
    return pending


def select_pending(items: list[dict[str, Any]], only_name: str | None, batch_size: int) -> tuple[list[dict[str, Any]], str | None]:
    pool = items
    if only_name is not None:
        pool = [
            item for item in items
            if str(item.get("Name") or item.get("Path") or "") == only_name
        ]
        if len(pool) != 1:
            return [], "selected_item_missing"
    return pool[:batch_size], None


def stable_source_id(item: dict[str, Any]) -> str:
    remote_id = item.get("ID")
    if isinstance(remote_id, str) and remote_id:
        return remote_id

    fallback = "|".join(
        [
            str(item.get("Path") or item.get("Name") or ""),
            str(item.get("Size") or ""),
            str(item.get("ModTime") or ""),
        ]
    )
    return "fallback-" + hashlib.sha256(fallback.encode("utf-8")).hexdigest()


def safe_package_id(source_id: str) -> str:
    return hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:20]


def safe_local_name(source_id: str, item: dict[str, Any]) -> str:
    suffix = video_suffix(item) or ".bin"
    return f"source_{safe_package_id(source_id)}{suffix}"


def remote_item_path(remote_root: str, folder: str, item: dict[str, Any]) -> str:
    item_path = str(item.get("Path") or item.get("Name") or "")
    return join_remote(remote_root, str(PurePosixPath(folder) / item_path))


def build_queue(
    remote_root: str,
    inbox: str,
    selected: list[dict[str, Any]],
    incoming_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    queue_items: list[dict[str, Any]] = []
    downloads: list[dict[str, Any]] = []
    incoming_dir.mkdir(parents=True, exist_ok=True)

    for item in selected:
        remote_name = str(item.get("Name") or item.get("Path") or "")
        source_id = stable_source_id(item)
        local_path = incoming_dir / safe_local_name(source_id, item)
        source_remote = remote_item_path(remote_root, inbox, item)
        record_item = {"ModTime": item.get("ModTime"), "MimeType": item.get("MimeType")}

        result = run_rclone(
            [
                "copyto",
                source_remote,
                str(local_path),
                "--log-level",
                "ERROR",
                "--stats",
                "0",
            ]
        )
        if result.returncode != 0 or not local_path.is_file():
            downloads.append(
                {
                    "source_id": source_id,
                    "package_id": safe_package_id(source_id),
                    "status": "error",
                    "error_code": "download_failed",
                    "remote_name": remote_name,
                    "remote_path": source_remote,
                    "record_item": record_item,
                }
            )
            continue

        queue_items.append({"source": str(local_path), "source_id": source_id})
        downloads.append(
            {
                "source_id": source_id,
                "package_id": safe_package_id(source_id),
                "status": "downloaded",
                "remote_name": remote_name,
                "remote_path": source_remote,
                "record_item": record_item,
            }
        )

    return queue_items, downloads


def run_batch(queue_path: Path, output_dir: Path, args: argparse.Namespace) -> dict[str, Any] | None:
    command = [
        sys.executable,
        str(Path(__file__).with_name("batch_ingest.py")),
        "--queue",
        str(queue_path),
        "--out",
        str(output_dir),
        "--batch-size",
        str(args.batch_size),
        "--max-keyframes",
        str(args.max_keyframes),
    ]
    if args.ocr:
        command.append("--ocr")
        command.extend(["--ocr-languages", args.ocr_languages])
    if args.transcribe:
        command.append("--transcribe")
        command.extend(["--whisper-model", args.whisper_model])
        if args.language:
            command.extend(["--language", args.language])

    result = subprocess.run(command, check=False, capture_output=True, text=True, env=os.environ.copy())
    if result.returncode != 0:
        return None

    summary_path = output_dir / "batch-summary.json"
    try:
        return json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def rewrite_private_manifest(package_dir: Path, provider: str, source_id: str, remote_name: str) -> bool:
    manifest_path = package_dir / "ingest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source = manifest.setdefault("source", {})
        source["provider"] = provider
        source["file_id"] = source_id
        source["name"] = remote_name
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return True
    except (OSError, json.JSONDecodeError, TypeError):
        return False


def refresh_private_evidence(package_dir: Path, provider: str, source_id: str) -> bool:
    try:
        evidence = build_evidence_summary(package_dir)
        source = evidence.get("source") if isinstance(evidence.get("source"), dict) else {}
        return source.get("provider") == provider and source.get("file_id") == source_id and "name" not in source
    except Exception:
        return False


def upload_package(remote_root: str, ready: str, package_id: str, package_dir: Path) -> bool:
    destination = join_remote(remote_root, str(PurePosixPath(ready) / package_id))
    result = run_rclone(["copy", str(package_dir), destination, "--log-level", "ERROR", "--stats", "0"])
    return result.returncode == 0


def upload_process_record(remote_root: str, process_log: str, package_id: str, record_path: Path) -> bool:
    destination = join_remote(remote_root, str(PurePosixPath(process_log) / f"{package_id}.json"))
    result = run_rclone(["copyto", str(record_path), destination, "--log-level", "ERROR", "--stats", "0"])
    return result.returncode == 0


def upload_package_record(remote_root: str, ready: str, package_id: str, record_path: Path) -> bool:
    destination = join_remote(remote_root, str(PurePosixPath(ready) / package_id / "process-record.json"))
    result = run_rclone(["copyto", str(record_path), destination, "--log-level", "ERROR", "--stats", "0"])
    return result.returncode == 0


def move_source(source_remote: str, remote_root: str, destination_folder: str, remote_name: str) -> bool:
    destination = join_remote(remote_root, str(PurePosixPath(destination_folder) / remote_name))
    result = run_rclone(["moveto", source_remote, destination, "--log-level", "ERROR", "--stats", "0"])
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge an rclone inbox to Technology Library ingestion.")
    parser.add_argument("--remote", required=True, help="Rclone root, e.g. tl: or fake:/absolute/root")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--provider", default="rclone")
    parser.add_argument("--inbox", default=DEFAULT_INBOX)
    parser.add_argument("--ready", default=DEFAULT_READY)
    parser.add_argument("--processed", default=DEFAULT_PROCESSED)
    parser.add_argument("--error", default=DEFAULT_ERROR)
    parser.add_argument("--process-log", default=DEFAULT_PROCESS_LOG)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--only-name", help="Process only this exact routed filename for a bounded local proof.")
    parser.add_argument("--max-keyframes", type=int, default=12)
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--ocr-languages", default="eng+por")
    parser.add_argument("--transcribe", action="store_true")
    parser.add_argument("--whisper-model", default="base")
    parser.add_argument("--language", default=None)
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("bridge_error code=rclone_missing")
        return 2
    if not 1 <= args.batch_size <= 20:
        parser.error("--batch-size must be between 1 and 20")

    capture_rclone_environment()

    for folder in (args.inbox, args.ready, args.processed, args.error, args.process_log):
        if not ensure_remote_dir(args.remote, folder):
            print("bridge_error code=remote_directory_unavailable")
            return 2

    pending = list_pending(args.remote, args.inbox)
    selected, selection_error = select_pending(pending, args.only_name, args.batch_size)
    if selection_error:
        print(f"bridge_error code={selection_error}")
        return 2
    if not selected:
        print("bridge_ok pending=0 processed=0 errors=0")
        return 0

    workspace = args.workspace.resolve()
    incoming_dir = workspace / "incoming"
    output_dir = workspace / "results"
    record_dir = workspace / "process-records"
    queue_path = workspace / "queue.json"
    workspace.mkdir(parents=True, exist_ok=True)

    queue_items, downloads = build_queue(args.remote, args.inbox, selected, incoming_dir)
    queue_path.write_text(
        json.dumps({"schema_version": 1, "items": queue_items}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    batch_summary = run_batch(queue_path, output_dir, args) if queue_items else {"pipeline_version": PIPELINE_VERSION, "results": []}
    batch_results = batch_summary.get("results", []) if batch_summary else []
    result_by_package = {
        result.get("package_id"): result
        for result in batch_results
        if isinstance(result, dict) and result.get("package_id")
    }

    sanitized_results: list[dict[str, Any]] = []
    processed_count = 0
    error_count = 0

    def persist_record(
        download: dict[str, Any],
        *,
        status: str,
        destination_folder: str | None,
        error_code: str | None = None,
        retryable: bool = False,
        package_dir: Path | None = None,
    ) -> tuple[Path, bool]:
        package_id = download["package_id"]
        record = build_process_record(
            provider=args.provider,
            source_id=download["source_id"],
            source_name=download["remote_name"],
            source_folder=args.inbox,
            package_id=package_id,
            pipeline_version=PIPELINE_VERSION,
            compactor_version=COMPACTOR_VERSION,
            status=status,
            item=download.get("record_item") or {},
            destination_folder=destination_folder,
            error_code=error_code,
            retryable=retryable,
            keep_original=False,
            source_recoverable=None,
            retention_days=30,
        )
        record_path = (package_dir / "process-record.json") if package_dir else (record_dir / f"{package_id}.json")
        write_process_record(record_path, record)
        return record_path, upload_process_record(args.remote, args.process_log, package_id, record_path)

    for download in downloads:
        package_id = download["package_id"]
        source_id = download["source_id"]
        source_remote = download["remote_path"]
        remote_name = download["remote_name"]

        if download["status"] == "error":
            _, logged = persist_record(download, status="error", destination_folder=args.inbox, error_code="download_failed", retryable=True)
            error_count += 1
            sanitized_results.append({
                "package_id": package_id,
                "status": "error",
                "error_code": "download_failed" if logged else "download_failed_process_log_sync_failed",
            })
            continue

        batch_result = result_by_package.get(package_id)
        if not batch_result or batch_result.get("status") not in {"processed", "skipped"}:
            moved = move_source(source_remote, args.remote, args.error, remote_name)
            error_code = "processing_failed" if moved else "processing_failed_source_not_moved"
            _, logged = persist_record(
                download,
                status="error",
                destination_folder=args.error if moved else args.inbox,
                error_code=error_code,
                retryable=True,
            )
            error_count += 1
            sanitized_results.append({
                "package_id": package_id,
                "status": "error",
                "error_code": error_code if logged else f"{error_code}_process_log_sync_failed",
            })
            continue

        package_dir = output_dir / package_id
        if not rewrite_private_manifest(package_dir, args.provider, source_id, remote_name):
            _, logged = persist_record(download, status="error", destination_folder=args.inbox, error_code="manifest_rewrite_failed", retryable=True)
            error_count += 1
            sanitized_results.append({"package_id": package_id, "status": "error", "error_code": "manifest_rewrite_failed" if logged else "manifest_rewrite_failed_process_log_sync_failed"})
            continue

        if not refresh_private_evidence(package_dir, args.provider, source_id):
            _, logged = persist_record(download, status="error", destination_folder=args.inbox, error_code="evidence_refresh_failed", retryable=True)
            error_count += 1
            sanitized_results.append({"package_id": package_id, "status": "error", "error_code": "evidence_refresh_failed" if logged else "evidence_refresh_failed_process_log_sync_failed"})
            continue

        success_record = build_process_record(
            provider=args.provider,
            source_id=source_id,
            source_name=remote_name,
            source_folder=args.inbox,
            package_id=package_id,
            pipeline_version=PIPELINE_VERSION,
            compactor_version=COMPACTOR_VERSION,
            status="processed",
            item=download.get("record_item") or {},
            destination_folder=args.processed,
            error_code=None,
            retryable=False,
            keep_original=False,
            source_recoverable=None,
            retention_days=30,
        )
        package_record_path = package_dir / "process-record.json"
        write_process_record(package_record_path, success_record)

        if not upload_package(args.remote, args.ready, package_id, package_dir):
            _, logged = persist_record(download, status="error", destination_folder=args.inbox, error_code="upload_failed", retryable=True)
            error_count += 1
            sanitized_results.append({"package_id": package_id, "status": "error", "error_code": "upload_failed" if logged else "upload_failed_process_log_sync_failed"})
            continue

        if not move_source(source_remote, args.remote, args.processed, remote_name):
            failure_record_path, logged = persist_record(
                download,
                status="error",
                destination_folder=args.inbox,
                error_code="source_move_failed",
                retryable=True,
                package_dir=package_dir,
            )
            package_record_synced = upload_package_record(args.remote, args.ready, package_id, failure_record_path)
            error_count += 1
            code = "source_move_failed"
            if not logged:
                code += "_process_log_sync_failed"
            if not package_record_synced:
                code += "_package_record_sync_failed"
            sanitized_results.append({"package_id": package_id, "status": "error", "error_code": code})
            continue

        process_log_synced = upload_process_record(args.remote, args.process_log, package_id, package_record_path)
        processed_count += 1
        sanitized: dict[str, Any] = {"package_id": package_id, "status": "processed"}
        if not process_log_synced:
            sanitized["warning"] = "process_log_sync_failed"
        sanitized_results.append(sanitized)

    bridge_summary = {
        "schema_version": 1,
        "pipeline_version": PIPELINE_VERSION,
        "selected": len(selected),
        "processed": processed_count,
        "errors": error_count,
        "results": sanitized_results,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    (workspace / "bridge-summary.json").write_text(
        json.dumps(bridge_summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("bridge_ok " f"pending={len(selected)} " f"processed={processed_count} " f"errors={error_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
