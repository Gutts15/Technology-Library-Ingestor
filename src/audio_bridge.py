#!/usr/bin/env python3
"""Provider-neutral bridge for the Technology Library audio queue.

Consumes supported audio files from TO_REVIEW/AUDIO, runs Audio V1 in-process so
faster-whisper can reuse its model cache across the batch, uploads compact
packages to READY_FOR_ANALYSIS, persists PROCESS_LOG records and moves originals
to PROCESSED/AUDIO. Unsupported formats remain untouched.

Storage credentials are removed from the processor environment before local
media work begins. Normal stdout contains counts and package IDs only, never
private filenames or transcript content.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from audio_ingest import (
    AUDIO_PIPELINE_VERSION,
    AUDIO_SUMMARY_VERSION,
    MAX_SUMMARY_BYTES,
    SUPPORTED_SUFFIXES,
    ingest_audio,
)
from process_record import build_process_record, write_process_record
from transcription_engine import DEFAULT_MODEL

DEFAULT_INBOX = "99_INBOX/TO_REVIEW/AUDIO"
DEFAULT_READY = "99_INBOX/READY_FOR_ANALYSIS"
DEFAULT_PROCESSED = "99_INBOX/PROCESSED/AUDIO"
DEFAULT_ERROR = "99_INBOX/ERROR/AUDIO"
DEFAULT_PROCESS_LOG = "99_INBOX/PROCESS_LOG/RECORDS"
DEFAULT_BATCH_SIZE = 5
MAX_BATCH_SIZE = 20
_RCLONE_ENV: dict[str, str] = {}

MIME_SUFFIXES = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
}


def capture_rclone_environment() -> None:
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
    return run_rclone(["mkdir", join_remote(remote_root, relative), "--log-level", "ERROR"]).returncode == 0


def remote_exists(remote_path: str) -> bool:
    return run_rclone(["lsjson", remote_path, "--stat", "--log-level", "ERROR"]).returncode == 0


def item_name(item: dict[str, Any]) -> str | None:
    raw = item.get("Name") or item.get("Path")
    if not isinstance(raw, str) or not raw:
        return None
    return PurePosixPath(raw).name


def item_suffix(item: dict[str, Any]) -> str:
    name = item_name(item)
    return Path(name).suffix.lower() if name else ""


def ingest_suffix(item: dict[str, Any]) -> str | None:
    suffix = item_suffix(item)
    if suffix in SUPPORTED_SUFFIXES:
        return suffix
    mime = str(item.get("MimeType") or "").lower().strip()
    return MIME_SUFFIXES.get(mime)


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


def list_supported(remote_root: str, inbox: str) -> list[dict[str, Any]] | None:
    result = run_rclone([
        "lsjson", join_remote(remote_root, inbox), "--files-only", "--log-level", "ERROR",
    ])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None

    output: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        suffix = ingest_suffix(item)
        if not suffix:
            continue
        copy = dict(item)
        copy["_ingest_suffix"] = suffix
        output.append(copy)
    output.sort(key=lambda value: (str(value.get("ModTime") or ""), str(value.get("Path") or value.get("Name") or "")))
    return output


def source_remote(remote_root: str, inbox: str, item: dict[str, Any]) -> str:
    raw = str(item.get("Path") or item.get("Name") or "")
    return join_remote(remote_root, str(PurePosixPath(inbox) / raw))


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
    return run_rclone([
        "moveto", source, destination, "--log-level", "ERROR", "--stats", "0",
    ]).returncode == 0


def upload_directory(remote_root: str, ready: str, pid: str, package_dir: Path) -> bool:
    destination = join_remote(remote_root, str(PurePosixPath(ready) / pid))
    return run_rclone([
        "copy", str(package_dir), destination, "--log-level", "ERROR", "--stats", "0",
    ]).returncode == 0


def upload_record(remote_root: str, records: str, pid: str, path: Path) -> bool:
    destination = join_remote(remote_root, str(PurePosixPath(records) / f"{pid}.json"))
    return run_rclone([
        "copyto", str(path), destination, "--log-level", "ERROR", "--stats", "0",
    ]).returncode == 0


def read_remote_json(remote_path: str) -> dict[str, Any] | None:
    result = run_rclone(["cat", remote_path, "--log-level", "ERROR"])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def ready_package_complete(remote_root: str, ready: str, pid: str) -> bool:
    base = str(PurePosixPath(ready) / pid)
    required = (
        "ingest.json",
        "checkpoint.json",
        "audio-index.json",
        "evidence-summary.json",
        "preview.opus",
        "process-record.json",
    )
    return all(remote_exists(join_remote(remote_root, str(PurePosixPath(base) / name))) for name in required)


def restore_central_record(remote_root: str, ready: str, records: str, pid: str) -> bool:
    source = join_remote(remote_root, str(PurePosixPath(ready) / pid / "process-record.json"))
    destination = join_remote(remote_root, str(PurePosixPath(records) / f"{pid}.json"))
    return run_rclone([
        "copyto", source, destination, "--log-level", "ERROR", "--stats", "0",
    ]).returncode == 0


def rewrite_private_metadata(package_dir: Path, provider: str, source_id: str, name: str) -> bool:
    try:
        manifest_path = package_dir / "ingest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source = manifest.setdefault("source", {})
        source["provider"] = provider
        source["file_id"] = source_id
        source["name"] = name
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        summary_path = package_dir / "evidence-summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary_source = summary.setdefault("source", {})
        summary_source["provider"] = provider
        summary_source["file_id"] = source_id
        summary_source.pop("name", None)
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        return summary_path.stat().st_size <= MAX_SUMMARY_BYTES
    except (OSError, json.JSONDecodeError, TypeError):
        return False


def build_record(
    *,
    provider: str,
    item: dict[str, Any],
    source_id: str,
    pid: str,
    inbox: str,
    destination: str,
    status: str,
    error_code: str | None,
    retryable: bool,
) -> dict[str, Any]:
    return build_process_record(
        provider=provider,
        source_id=source_id,
        source_name=item_name(item) or "source",
        source_folder=inbox,
        package_id=pid,
        pipeline_version=AUDIO_PIPELINE_VERSION,
        compactor_version=AUDIO_SUMMARY_VERSION,
        status=status,
        item={"ModTime": item.get("ModTime"), "MimeType": item.get("MimeType")},
        destination_folder=destination,
        error_code=error_code,
        retryable=retryable,
        keep_original=False,
        source_recoverable=None,
        retention_days=30,
    )


def persist_error_record(
    *,
    args: argparse.Namespace,
    workspace: Path,
    item: dict[str, Any],
    source_id: str,
    pid: str,
    destination: str,
    error_code: str,
) -> bool:
    record = build_record(
        provider=args.provider,
        item=item,
        source_id=source_id,
        pid=pid,
        inbox=args.inbox,
        destination=destination,
        status="error",
        error_code=error_code,
        retryable=True,
    )
    path = workspace / "records" / f"{pid}.json"
    write_process_record(path, record)
    return upload_record(args.remote, args.process_log, pid, path)


def process_one(item: dict[str, Any], args: argparse.Namespace, workspace: Path) -> dict[str, Any]:
    name = item_name(item)
    if not name:
        return {"package_id": None, "status": "error", "error_code": "invalid_name"}

    source_id = stable_source_id(item)
    pid = package_id(source_id)
    remote_source = source_remote(args.remote, args.inbox, item)

    if ready_package_complete(args.remote, args.ready, pid):
        record_path = join_remote(args.remote, str(PurePosixPath(args.ready) / pid / "process-record.json"))
        record = read_remote_json(record_path)
        if record and record.get("processing", {}).get("status") == "processed":
            if move_source(remote_source, args.remote, args.processed, name, pid):
                restore_central_record(args.remote, args.ready, args.process_log, pid)
                return {"package_id": pid, "status": "finalized", "reason": "ready_package_reused"}

    incoming = workspace / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    local_source = incoming / safe_local_name(source_id, item)
    download = run_rclone([
        "copyto", remote_source, str(local_source), "--log-level", "ERROR", "--stats", "0",
    ])
    if download.returncode != 0 or not local_source.is_file():
        persist_error_record(
            args=args,
            workspace=workspace,
            item=item,
            source_id=source_id,
            pid=pid,
            destination=args.inbox,
            error_code="download_failed",
        )
        return {"package_id": pid, "status": "error", "error_code": "download_failed"}

    package_dir = workspace / "results" / pid
    try:
        ingest_audio(
            local_source,
            package_dir,
            source_id=source_id,
            transcribe=args.transcribe,
            model_name=args.whisper_model,
            language=args.language,
        )
    except (RuntimeError, OSError, ValueError, OverflowError):
        moved = move_source(remote_source, args.remote, args.error, name, pid)
        destination = args.error if moved else args.inbox
        persist_error_record(
            args=args,
            workspace=workspace,
            item=item,
            source_id=source_id,
            pid=pid,
            destination=destination,
            error_code="audio_processing_failed",
        )
        return {"package_id": pid, "status": "error", "error_code": "audio_processing_failed"}

    if not rewrite_private_metadata(package_dir, args.provider, source_id, name):
        persist_error_record(
            args=args,
            workspace=workspace,
            item=item,
            source_id=source_id,
            pid=pid,
            destination=args.inbox,
            error_code="metadata_rewrite_failed",
        )
        return {"package_id": pid, "status": "error", "error_code": "metadata_rewrite_failed"}

    record = build_record(
        provider=args.provider,
        item=item,
        source_id=source_id,
        pid=pid,
        inbox=args.inbox,
        destination=args.processed,
        status="processed",
        error_code=None,
        retryable=False,
    )
    package_record = package_dir / "process-record.json"
    write_process_record(package_record, record)

    if not upload_directory(args.remote, args.ready, pid, package_dir):
        persist_error_record(
            args=args,
            workspace=workspace,
            item=item,
            source_id=source_id,
            pid=pid,
            destination=args.inbox,
            error_code="upload_failed",
        )
        return {"package_id": pid, "status": "error", "error_code": "upload_failed"}

    if not move_source(remote_source, args.remote, args.processed, name, pid):
        failure = build_record(
            provider=args.provider,
            item=item,
            source_id=source_id,
            pid=pid,
            inbox=args.inbox,
            destination=args.inbox,
            status="error",
            error_code="source_move_failed",
            retryable=True,
        )
        write_process_record(package_record, failure)
        upload_record(args.remote, args.process_log, pid, package_record)
        package_record_remote = join_remote(
            args.remote,
            str(PurePosixPath(args.ready) / pid / "process-record.json"),
        )
        run_rclone([
            "copyto", str(package_record), package_record_remote,
            "--log-level", "ERROR", "--stats", "0",
        ])
        return {"package_id": pid, "status": "error", "error_code": "source_move_failed"}

    logged = upload_record(args.remote, args.process_log, pid, package_record)
    payload: dict[str, Any] = {"package_id": pid, "status": "processed"}
    if not logged:
        payload["warning"] = "process_log_sync_failed"
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Process supported audio files from a private rclone queue.")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--provider", default="rclone")
    parser.add_argument("--inbox", default=DEFAULT_INBOX)
    parser.add_argument("--ready", default=DEFAULT_READY)
    parser.add_argument("--processed", default=DEFAULT_PROCESSED)
    parser.add_argument("--error", default=DEFAULT_ERROR)
    parser.add_argument("--process-log", default=DEFAULT_PROCESS_LOG)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--transcribe", action="store_true")
    parser.add_argument("--whisper-model", default=DEFAULT_MODEL)
    parser.add_argument("--language")
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("audio_bridge_error code=rclone_missing")
        return 2
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        parser.error(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")

    capture_rclone_environment()
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    for folder in (args.inbox, args.ready, args.processed, args.error, args.process_log):
        if not ensure_remote_dir(args.remote, folder):
            print("audio_bridge_error code=remote_directory_unavailable")
            return 2

    items = list_supported(args.remote, args.inbox)
    if items is None:
        print("audio_bridge_error code=list_failed")
        return 2

    selected = items[: args.batch_size]
    results: list[dict[str, Any]] = []
    for item in selected:
        results.append(process_one(item, args, workspace))

    processed = sum(1 for item in results if item.get("status") == "processed")
    finalized = sum(1 for item in results if item.get("status") == "finalized")
    errors = sum(1 for item in results if item.get("status") == "error")
    warnings = sum(1 for item in results if item.get("warning"))
    summary = {
        "pipeline_version": AUDIO_PIPELINE_VERSION,
        "pending": len(items),
        "selected": len(selected),
        "processed": processed,
        "finalized": finalized,
        "errors": errors,
        "warnings": warnings,
        "packages": [
            item.get("package_id")
            for item in results
            if isinstance(item.get("package_id"), str)
        ],
    }
    (workspace / "bridge-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if errors:
        print(
            "audio_bridge_error code=batch_failed "
            f"pending={len(items)} selected={len(selected)} processed={processed} "
            f"finalized={finalized} errors={errors} warnings={warnings}"
        )
        return 2

    print(
        "audio_bridge_ok "
        f"pending={len(items)} selected={len(selected)} processed={processed} "
        f"finalized={finalized} errors=0 warnings={warnings}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
