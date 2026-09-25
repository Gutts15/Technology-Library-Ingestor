#!/usr/bin/env python3
"""Provider-neutral bridge for Technology Library text/document queues.

Consumes supported files already routed from DROP_HERE into TO_REVIEW/TEXT and
TO_REVIEW/DOCUMENTS, runs local extraction, writes compact packages to
READY_FOR_ANALYSIS, persists PROCESS_LOG records, and moves originals to their
PROCESSED queue. It prints only counts and package IDs, never private filenames
or extracted content.
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
from pathlib import Path, PurePosixPath
from typing import Any

from process_record import build_process_record, write_process_record
from text_document_ingest import (
    CONTENT_PIPELINE_VERSION,
    CONTENT_SUMMARY_VERSION,
    DOCUMENT_SUFFIXES,
    MAX_SUMMARY_BYTES,
    TEXT_SUFFIXES,
)

DEFAULT_TEXT_INBOX = "99_INBOX/TO_REVIEW/TEXT"
DEFAULT_DOCUMENT_INBOX = "99_INBOX/TO_REVIEW/DOCUMENTS"
DEFAULT_READY = "99_INBOX/READY_FOR_ANALYSIS"
DEFAULT_TEXT_PROCESSED = "99_INBOX/PROCESSED/TEXT"
DEFAULT_DOCUMENT_PROCESSED = "99_INBOX/PROCESSED/DOCUMENTS"
DEFAULT_TEXT_ERROR = "99_INBOX/ERROR/TEXT"
DEFAULT_DOCUMENT_ERROR = "99_INBOX/ERROR/DOCUMENTS"
DEFAULT_PROCESS_LOG = "99_INBOX/PROCESS_LOG/RECORDS"
DEFAULT_BATCH_SIZE = 10
MAX_BATCH_SIZE = 30
_RCLONE_ENV: dict[str, str] = {}

TEXT_MIME_TYPES = {
    "application/json", "application/ld+json", "application/xml", "application/yaml",
    "application/x-yaml", "application/javascript", "application/x-javascript",
    "application/toml", "application/sql",
}
DOCUMENT_MIME_SUFFIXES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}


def capture_rclone_environment() -> None:
    for key in list(os.environ):
        if key == "RCLONE_CONFIG" or key.startswith("RCLONE_CONFIG_") or key.startswith("RCLONE_DRIVE_"):
            _RCLONE_ENV[key] = os.environ.pop(key)


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env.update(_RCLONE_ENV)
    return subprocess.run(["rclone", *arguments], check=False, capture_output=True, env=env)


def ensure_remote_dir(remote_root: str, relative: str) -> bool:
    return run_rclone(["mkdir", join_remote(remote_root, relative), "--log-level", "ERROR"]).returncode == 0


def remote_exists(remote_path: str) -> bool:
    return run_rclone(["lsjson", remote_path, "--stat", "--log-level", "ERROR"]).returncode == 0


def stable_source_id(item: dict[str, Any]) -> str:
    remote_id = item.get("ID")
    if isinstance(remote_id, str) and remote_id:
        return remote_id
    fallback = "|".join([
        str(item.get("Path") or item.get("Name") or ""),
        str(item.get("Size") or ""),
        str(item.get("ModTime") or ""),
    ])
    return "fallback-" + hashlib.sha256(fallback.encode("utf-8")).hexdigest()


def package_id(source_id: str) -> str:
    return hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:20]


def item_name(item: dict[str, Any]) -> str | None:
    raw = item.get("Name") or item.get("Path")
    if not isinstance(raw, str) or not raw:
        return None
    return PurePosixPath(raw).name


def item_suffix(item: dict[str, Any]) -> str:
    name = item_name(item)
    return Path(name).suffix.lower() if name else ""


def ingest_suffix(item: dict[str, Any], allowed: set[str], kind: str) -> str | None:
    suffix = item_suffix(item)
    if suffix in allowed:
        return suffix
    mime = str(item.get("MimeType") or "").lower().strip()
    if kind == "text" and (mime.startswith("text/") or mime in TEXT_MIME_TYPES):
        return ".txt"
    if kind == "document":
        return DOCUMENT_MIME_SUFFIXES.get(mime)
    return None


def list_supported(remote_root: str, folder: str, allowed: set[str], kind: str) -> list[dict[str, Any]] | None:
    result = run_rclone(["lsjson", join_remote(remote_root, folder), "--files-only", "--log-level", "ERROR"])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, list):
        return None
    output: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        suffix = ingest_suffix(item, allowed, kind)
        if not suffix:
            continue
        copy = dict(item)
        copy["_queue_kind"] = kind
        copy["_source_folder"] = folder
        copy["_ingest_suffix"] = suffix
        output.append(copy)
    output.sort(key=lambda value: (str(value.get("ModTime") or ""), str(value.get("Path") or value.get("Name") or "")))
    return output


def source_remote(remote_root: str, folder: str, item: dict[str, Any]) -> str:
    raw = str(item.get("Path") or item.get("Name") or "")
    return join_remote(remote_root, str(PurePosixPath(folder) / raw))


def safe_local_name(source_id: str, item: dict[str, Any]) -> str:
    suffix = str(item.get("_ingest_suffix") or item_suffix(item) or ".bin")
    return f"source_{package_id(source_id)}{suffix}"


def safe_destination(remote_root: str, folder: str, name: str, pid: str) -> str:
    normal = join_remote(remote_root, str(PurePosixPath(folder) / name))
    if not remote_exists(normal):
        return normal
    path = PurePosixPath(name)
    alternate = f"{path.stem}.{pid}{path.suffix}"
    return join_remote(remote_root, str(PurePosixPath(folder) / alternate))


def move_source(source: str, remote_root: str, folder: str, name: str, pid: str) -> bool:
    destination = safe_destination(remote_root, folder, name, pid)
    return run_rclone(["moveto", source, destination, "--log-level", "ERROR", "--stats", "0"]).returncode == 0


def upload_directory(remote_root: str, ready: str, pid: str, local_dir: Path) -> bool:
    destination = join_remote(remote_root, str(PurePosixPath(ready) / pid))
    return run_rclone(["copy", str(local_dir), destination, "--log-level", "ERROR", "--stats", "0"]).returncode == 0


def upload_record(remote_root: str, records: str, pid: str, path: Path) -> bool:
    destination = join_remote(remote_root, str(PurePosixPath(records) / f"{pid}.json"))
    return run_rclone(["copyto", str(path), destination, "--log-level", "ERROR", "--stats", "0"]).returncode == 0


def read_remote_json(remote_path: str) -> dict[str, Any] | None:
    result = run_rclone(["cat", remote_path, "--log-level", "ERROR"])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def ready_package_complete(remote_root: str, ready: str, pid: str) -> bool:
    base = str(PurePosixPath(ready) / pid)
    required = ("ingest.json", "checkpoint.json", "evidence-summary.json", "content-index.json", "process-record.json")
    return all(remote_exists(join_remote(remote_root, str(PurePosixPath(base) / name))) for name in required)


def restore_central_record(remote_root: str, ready: str, records: str, pid: str) -> bool:
    source = join_remote(remote_root, str(PurePosixPath(ready) / pid / "process-record.json"))
    destination = join_remote(remote_root, str(PurePosixPath(records) / f"{pid}.json"))
    return run_rclone(["copyto", source, destination, "--log-level", "ERROR", "--stats", "0"]).returncode == 0


def rewrite_private_metadata(package_dir: Path, provider: str, source_id: str, name: str) -> bool:
    try:
        manifest_path = package_dir / "ingest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source = manifest.setdefault("source", {})
        source["provider"] = provider
        source["file_id"] = source_id
        source["name"] = name
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        summary_path = package_dir / "evidence-summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary_source = summary.setdefault("source", {})
        summary_source["provider"] = provider
        summary_source["file_id"] = source_id
        summary_source.pop("name", None)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        return summary_path.stat().st_size <= MAX_SUMMARY_BYTES
    except (OSError, json.JSONDecodeError, TypeError):
        return False


def persist_error_record(*, remote_root: str, records: str, workspace: Path, provider: str,
                         item: dict[str, Any], source_id: str, pid: str, source_folder: str,
                         destination_folder: str, error_code: str, retryable: bool) -> bool:
    record = build_process_record(
        provider=provider, source_id=source_id, source_name=item_name(item) or "source",
        source_folder=source_folder, package_id=pid, pipeline_version=CONTENT_PIPELINE_VERSION,
        compactor_version=CONTENT_SUMMARY_VERSION, status="error",
        item={"ModTime": item.get("ModTime"), "MimeType": item.get("MimeType")},
        destination_folder=destination_folder, error_code=error_code, retryable=retryable,
        keep_original=False, source_recoverable=None, retention_days=30,
    )
    record_path = workspace / "records" / f"{pid}.json"
    write_process_record(record_path, record)
    return upload_record(remote_root, records, pid, record_path)


def process_one(item: dict[str, Any], args: argparse.Namespace, workspace: Path) -> dict[str, Any]:
    kind = str(item.get("_queue_kind"))
    source_folder = str(item.get("_source_folder"))
    processed_folder = args.text_processed if kind == "text" else args.document_processed
    error_folder = args.text_error if kind == "text" else args.document_error
    name = item_name(item)
    if not name:
        return {"package_id": None, "status": "error", "error_code": "invalid_name"}

    source_id = stable_source_id(item)
    pid = package_id(source_id)
    remote_source = source_remote(args.remote, source_folder, item)

    if ready_package_complete(args.remote, args.ready, pid):
        record_path = join_remote(args.remote, str(PurePosixPath(args.ready) / pid / "process-record.json"))
        record = read_remote_json(record_path)
        if record and record.get("processing", {}).get("status") == "processed":
            if move_source(remote_source, args.remote, processed_folder, name, pid):
                restore_central_record(args.remote, args.ready, args.process_log, pid)
                return {"package_id": pid, "status": "finalized", "reason": "ready_package_reused"}

    incoming = workspace / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    local_source = incoming / safe_local_name(source_id, item)
    download = run_rclone(["copyto", remote_source, str(local_source), "--log-level", "ERROR", "--stats", "0"])
    if download.returncode != 0 or not local_source.is_file():
        persist_error_record(remote_root=args.remote, records=args.process_log, workspace=workspace,
                             provider=args.provider, item=item, source_id=source_id, pid=pid,
                             source_folder=source_folder, destination_folder=source_folder,
                             error_code="download_failed", retryable=True)
        return {"package_id": pid, "status": "error", "error_code": "download_failed"}

    package_dir = workspace / "results" / pid
    command = [sys.executable, str(Path(__file__).with_name("text_document_ingest.py")), str(local_source),
               "--out", str(package_dir), "--source-id", source_id]
    result = subprocess.run(command, check=False, capture_output=True, text=True, env=os.environ.copy())
    if result.returncode != 0:
        moved = move_source(remote_source, args.remote, error_folder, name, pid)
        destination = error_folder if moved else source_folder
        persist_error_record(remote_root=args.remote, records=args.process_log, workspace=workspace,
                             provider=args.provider, item=item, source_id=source_id, pid=pid,
                             source_folder=source_folder, destination_folder=destination,
                             error_code="content_processing_failed", retryable=True)
        return {"package_id": pid, "status": "error", "error_code": "content_processing_failed"}

    if not rewrite_private_metadata(package_dir, args.provider, source_id, name):
        persist_error_record(remote_root=args.remote, records=args.process_log, workspace=workspace,
                             provider=args.provider, item=item, source_id=source_id, pid=pid,
                             source_folder=source_folder, destination_folder=source_folder,
                             error_code="metadata_rewrite_failed", retryable=True)
        return {"package_id": pid, "status": "error", "error_code": "metadata_rewrite_failed"}

    record = build_process_record(
        provider=args.provider, source_id=source_id, source_name=name, source_folder=source_folder,
        package_id=pid, pipeline_version=CONTENT_PIPELINE_VERSION, compactor_version=CONTENT_SUMMARY_VERSION,
        status="processed", item={"ModTime": item.get("ModTime"), "MimeType": item.get("MimeType")},
        destination_folder=processed_folder, error_code=None, retryable=False,
        keep_original=False, source_recoverable=None, retention_days=30,
    )
    package_record = package_dir / "process-record.json"
    write_process_record(package_record, record)

    if not upload_directory(args.remote, args.ready, pid, package_dir):
        persist_error_record(remote_root=args.remote, records=args.process_log, workspace=workspace,
                             provider=args.provider, item=item, source_id=source_id, pid=pid,
                             source_folder=source_folder, destination_folder=source_folder,
                             error_code="upload_failed", retryable=True)
        return {"package_id": pid, "status": "error", "error_code": "upload_failed"}

    if not move_source(remote_source, args.remote, processed_folder, name, pid):
        failure = build_process_record(
            provider=args.provider, source_id=source_id, source_name=name, source_folder=source_folder,
            package_id=pid, pipeline_version=CONTENT_PIPELINE_VERSION,
            compactor_version=CONTENT_SUMMARY_VERSION, status="error",
            item={"ModTime": item.get("ModTime"), "MimeType": item.get("MimeType")},
            destination_folder=source_folder, error_code="source_move_failed", retryable=True,
            keep_original=False, source_recoverable=None, retention_days=30,
        )
        write_process_record(package_record, failure)
        upload_record(args.remote, args.process_log, pid, package_record)
        package_record_remote = join_remote(args.remote, str(PurePosixPath(args.ready) / pid / "process-record.json"))
        run_rclone(["copyto", str(package_record), package_record_remote, "--log-level", "ERROR", "--stats", "0"])
        return {"package_id": pid, "status": "error", "error_code": "source_move_failed"}

    logged = upload_record(args.remote, args.process_log, pid, package_record)
    payload: dict[str, Any] = {"package_id": pid, "status": "processed", "kind": kind}
    if not logged:
        payload["warning"] = "process_log_sync_failed"
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest routed text and supported documents from private rclone storage.")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--provider", default="rclone")
    parser.add_argument("--text-inbox", default=DEFAULT_TEXT_INBOX)
    parser.add_argument("--document-inbox", default=DEFAULT_DOCUMENT_INBOX)
    parser.add_argument("--ready", default=DEFAULT_READY)
    parser.add_argument("--text-processed", default=DEFAULT_TEXT_PROCESSED)
    parser.add_argument("--document-processed", default=DEFAULT_DOCUMENT_PROCESSED)
    parser.add_argument("--text-error", default=DEFAULT_TEXT_ERROR)
    parser.add_argument("--document-error", default=DEFAULT_DOCUMENT_ERROR)
    parser.add_argument("--process-log", default=DEFAULT_PROCESS_LOG)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--only-name", help="Process only this exact routed filename for a bounded local proof.")
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("content_bridge_error code=rclone_missing")
        return 2
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        parser.error(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")

    capture_rclone_environment()
    folders = (args.text_inbox, args.document_inbox, args.ready, args.text_processed,
               args.document_processed, args.text_error, args.document_error, args.process_log)
    for folder in folders:
        if not ensure_remote_dir(args.remote, folder):
            print("content_bridge_error code=remote_directory_unavailable")
            return 2

    text_items = list_supported(args.remote, args.text_inbox, TEXT_SUFFIXES, "text")
    document_items = list_supported(args.remote, args.document_inbox, DOCUMENT_SUFFIXES, "document")
    if text_items is None or document_items is None:
        print("content_bridge_error code=list_failed")
        return 2

    all_items = sorted([*text_items, *document_items],
                       key=lambda value: (str(value.get("ModTime") or ""), str(value.get("Path") or value.get("Name") or "")))
    if args.only_name is not None:
        all_items = [item for item in all_items if item_name(item) == args.only_name]
        if len(all_items) != 1:
            print("content_bridge_error code=selected_item_missing")
            return 2
    selected = all_items[:args.batch_size]
    if not selected:
        print("content_bridge_ok pending=0 processed=0 finalized=0 errors=0")
        return 0

    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    results = [process_one(item, args, workspace) for item in selected]
    processed = sum(result.get("status") == "processed" for result in results)
    finalized = sum(result.get("status") == "finalized" for result in results)
    errors = sum(result.get("status") == "error" for result in results)
    summary = {
        "schema_version": 1,
        "pipeline_version": CONTENT_PIPELINE_VERSION,
        "summary_version": CONTENT_SUMMARY_VERSION,
        "selected": len(selected),
        "processed": processed,
        "finalized": finalized,
        "errors": errors,
        "results": results,
    }
    (workspace / "content-bridge-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"content_bridge_ok pending={len(selected)} processed={processed} finalized={finalized} errors={errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
