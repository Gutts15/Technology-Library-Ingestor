#!/usr/bin/env python3
"""Audit and metadata-backfill legacy READY packages without reprocessing media.

The migration is intentionally conservative:
- it never regenerates OCR, transcripts, keyframes, previews or parsed content;
- it only repairs missing/obsolete `process-record.json` metadata when the
  package already satisfies the current artifact contract apart from that file;
- existing invalid process records are preserved before replacement;
- stdout contains aggregate structural counts only, never filenames, source
  names, provider IDs, URLs, transcripts or package IDs.

Dry-run is the default. Pass `--apply` explicitly to write changes.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from package_contracts import required_package_files

BACKFILL_VERSION = "0.1.0"
SCHEMA_VERSION = 1
DEFAULT_SOURCE = "99_INBOX/READY_FOR_ANALYSIS"
DEFAULT_PROCESS_LOG = "99_INBOX/PROCESS_LOG/RECORDS"
DEFAULT_LEGACY_BACKUP = "99_INBOX/PROCESS_LOG/LEGACY_BACKFILL"
DEFAULT_MAX_PACKAGES = 100
MAX_MAX_PACKAGES = 500
PACKAGE_ID_RE = re.compile(r"^[0-9a-f]{20}$")
SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9._+-]{1,48}$")

KIND_TO_QUEUE = {
    "video": "99_INBOX/TO_REVIEW/VIDEOS",
    "image": "99_INBOX/TO_REVIEW/IMAGES",
    "audio": "99_INBOX/TO_REVIEW/AUDIO",
    "text": "99_INBOX/TO_REVIEW/TEXT",
    "document": "99_INBOX/TO_REVIEW/DOCUMENTS",
    "spreadsheet": "99_INBOX/TO_REVIEW/SPREADSHEETS",
    "link": "99_INBOX/TO_REVIEW/LINKS",
}
KIND_TO_PROCESSED = {
    "video": "99_INBOX/PROCESSED/VIDEOS",
    "image": "99_INBOX/PROCESSED/IMAGES",
    "audio": "99_INBOX/PROCESSED/AUDIO",
    "text": "99_INBOX/PROCESSED/TEXT",
    "document": "99_INBOX/PROCESSED/DOCUMENTS",
    "spreadsheet": "99_INBOX/PROCESSED/SPREADSHEETS",
    "link": "99_INBOX/PROCESSED/LINKS",
}
DOCUMENT_SUFFIXES = {".pdf", ".docx", ".pptx"}
DOCUMENT_MIME_HINTS = (
    "application/pdf",
    "wordprocessingml",
    "presentationml",
    "msword",
    "powerpoint",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *arguments], check=False, capture_output=True, text=True
    )


def read_json_text(text: str | None) -> dict[str, Any] | None:
    if not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return read_json_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None


def remote_text(remote_path: str) -> str | None:
    result = run_rclone(["cat", remote_path, "--log-level", "ERROR"])
    return result.stdout if result.returncode == 0 else None


def remote_json(remote_path: str) -> dict[str, Any] | None:
    return read_json_text(remote_text(remote_path))


def remote_entries(remote_path: str) -> list[dict[str, Any]] | None:
    result = run_rclone([
        "lsjson", remote_path, "--max-depth", "1", "--log-level", "ERROR"
    ])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    return [item for item in payload if isinstance(item, dict)]


def safe_string(value: Any, *, max_len: int = 1024) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:max_len] if value else None


def safe_version(value: Any, fallback: str = "legacy") -> str:
    value = safe_string(value, max_len=48)
    return value if value and SAFE_VERSION_RE.fullmatch(value) else fallback


def current_record_valid(record: dict[str, Any] | None, package_id: str) -> bool:
    return bool(
        isinstance(record, dict)
        and record.get("schema_version") == SCHEMA_VERSION
        and record.get("package_id") == package_id
    )


def infer_kind(filenames: set[str], ingest: dict[str, Any] | None) -> str | None:
    exact_markers = {
        "image-index.json": "image",
        "audio-index.json": "audio",
        "spreadsheet-index.json": "spreadsheet",
        "link-index.json": "link",
    }
    found = {kind for marker, kind in exact_markers.items() if marker in filenames}
    if len(found) == 1:
        return next(iter(found))
    if len(found) > 1:
        return None

    source = ingest.get("source") if isinstance(ingest, dict) and isinstance(ingest.get("source"), dict) else {}
    media_type = str(source.get("media_type") or "").lower()
    name = str(source.get("name") or "").lower()

    if "content-index.json" in filenames:
        if Path(name).suffix.lower() in DOCUMENT_SUFFIXES or any(hint in media_type for hint in DOCUMENT_MIME_HINTS):
            return "document"
        return "text"

    if media_type.startswith("image/"):
        return "image"
    if media_type.startswith("audio/"):
        return "audio"
    if media_type.startswith("video/"):
        return "video"

    if "timeline.md" in filenames or "contact-sheet.jpg" in filenames:
        return "video"

    if isinstance(ingest, dict):
        artifacts = ingest.get("artifacts") if isinstance(ingest.get("artifacts"), dict) else {}
        media = ingest.get("media") if isinstance(ingest.get("media"), dict) else {}
        if artifacts.get("timeline") or "duration_seconds" in media:
            return "video"

    return None


def record_reason(record_text: str | None, record: dict[str, Any] | None, package_id: str) -> str:
    if record_text is None:
        return "missing_process_record"
    if record is None:
        return "malformed_process_record"
    if record.get("schema_version") != SCHEMA_VERSION:
        return "obsolete_process_schema"
    if record.get("package_id") != package_id:
        return "package_id_mismatch"
    return "current"


def package_audit(
    *,
    package_id: str,
    filenames: set[str],
    process_text: str | None,
    ingest: dict[str, Any] | None,
) -> dict[str, Any]:
    process_record = read_json_text(process_text)
    reason = record_reason(process_text, process_record, package_id)
    if reason == "current":
        return {
            "state": "current",
            "reason": reason,
            "kind": None,
            "queue": None,
            "missing": (),
            "has_old_record": True,
        }

    kind = infer_kind(filenames, ingest)
    if kind is None:
        return {
            "state": "blocked",
            "reason": "kind_not_inferable",
            "kind": None,
            "queue": None,
            "missing": (),
            "has_old_record": process_text is not None,
        }

    queue = KIND_TO_QUEUE[kind]
    required = required_package_files(queue)
    if required is None:
        return {
            "state": "blocked",
            "reason": "contract_unknown",
            "kind": kind,
            "queue": queue,
            "missing": (),
            "has_old_record": process_text is not None,
        }

    required_without_record = {name for name in required if name != "process-record.json"}
    missing = tuple(sorted(required_without_record - filenames))
    if ingest is None:
        missing = tuple(sorted(set(missing) | {"ingest.json"}))

    return {
        "state": "backfillable" if not missing else "blocked",
        "reason": reason if not missing else "required_artifact_missing",
        "kind": kind,
        "queue": queue,
        "missing": missing,
        "has_old_record": process_text is not None,
    }


def build_backfill_record(
    *, package_id: str, kind: str, ingest: dict[str, Any], evidence: dict[str, Any] | None
) -> dict[str, Any]:
    source = ingest.get("source") if isinstance(ingest.get("source"), dict) else {}
    processing = ingest.get("processing") if isinstance(ingest.get("processing"), dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}

    original_status = processing.get("status")
    status = original_status if original_status in {"processed", "error"} else "processed"
    provider = safe_string(source.get("provider"), max_len=64) or "legacy"
    file_id = safe_string(source.get("file_id"), max_len=1024) or f"legacy-package:{package_id}"
    source_name = safe_string(source.get("name"), max_len=1024)
    media_type = safe_string(source.get("media_type"), max_len=256)
    processed_at = safe_string(processing.get("processed_at"), max_len=64)

    return {
        "schema_version": SCHEMA_VERSION,
        "record_version": "0.1.0",
        "source": {
            "provider": provider,
            "file_id": file_id,
            "name": source_name,
            "media_type": media_type,
            "source_folder": KIND_TO_QUEUE[kind],
            "received_at": None,
        },
        "package_id": package_id,
        "processing": {
            "status": status,
            "error_code": None,
            "retryable": False,
            "processed_at": processed_at,
            "pipeline_version": safe_version(processing.get("pipeline_version")),
            "compactor_version": safe_version(evidence.get("compactor_version")),
        },
        "routing": {
            "destination_folder": KIND_TO_PROCESSED[kind],
            "keep_original": False,
            "source_recoverable": None,
            "retention_days": 30,
        },
        "migration": {
            "type": "legacy_ready_metadata_backfill",
            "backfill_version": BACKFILL_VERSION,
            "migrated_at": utc_now(),
            "media_reprocessed": False,
            "kind_inferred": True,
            "status_inferred": original_status not in {"processed", "error"},
            "source_id_fallback": safe_string(source.get("file_id"), max_len=1024) is None,
            "source_name_missing": source_name is None,
        },
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def list_local_packages(root: Path, max_packages: int) -> list[tuple[str, Path]]:
    if not root.is_dir():
        return []
    packages = [p for p in root.iterdir() if p.is_dir() and PACKAGE_ID_RE.fullmatch(p.name)]
    packages.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [(p.name, p) for p in packages[:max_packages]]


def list_remote_packages(remote: str, source: str, max_packages: int) -> list[str] | None:
    entries = remote_entries(join_remote(remote, source))
    if entries is None:
        return None
    packages: list[tuple[str, str]] = []
    for item in entries:
        if not item.get("IsDir"):
            continue
        name = item.get("Name")
        if not isinstance(name, str) or not PACKAGE_ID_RE.fullmatch(name):
            continue
        packages.append((str(item.get("ModTime") or ""), name))
    packages.sort(reverse=True)
    return [name for _, name in packages[:max_packages]]


def summarize(audits: list[dict[str, Any]]) -> str:
    states = Counter(str(item.get("state") or "unknown") for item in audits)
    reasons = Counter(str(item.get("reason") or "unknown") for item in audits if item.get("state") != "current")
    kinds = Counter(str(item.get("kind")) for item in audits if item.get("kind"))
    reason_text = ",".join(f"{key}:{reasons[key]}" for key in sorted(reasons)) or "none"
    kind_text = ",".join(f"{key}:{kinds[key]}" for key in sorted(kinds)) or "none"
    return (
        f"seen={len(audits)} current={states['current']} backfillable={states['backfillable']} "
        f"blocked={states['blocked']} reasons={reason_text} kinds={kind_text}"
    )


def process_local(root: Path, process_log: Path, legacy_backup: Path, max_packages: int, apply: bool) -> tuple[int, list[dict[str, Any]]]:
    audits: list[dict[str, Any]] = []
    updated = 0
    for package_id, package in list_local_packages(root, max_packages):
        filenames = {p.name for p in package.iterdir() if p.is_file()}
        process_path = package / "process-record.json"
        try:
            process_text = process_path.read_text(encoding="utf-8") if process_path.is_file() else None
        except (OSError, UnicodeError):
            process_text = None
        ingest = read_json_file(package / "ingest.json")
        audit = package_audit(package_id=package_id, filenames=filenames, process_text=process_text, ingest=ingest)
        audits.append(audit)
        if not apply or audit["state"] != "backfillable" or ingest is None:
            continue

        if process_text is not None:
            backup_path = legacy_backup / f"{package_id}-process-record.json"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_text(process_text, encoding="utf-8")

        evidence = read_json_file(package / "evidence-summary.json")
        record = build_backfill_record(package_id=package_id, kind=audit["kind"], ingest=ingest, evidence=evidence)
        write_json(process_path, record)
        write_json(process_log / f"{package_id}.json", record)
        updated += 1
    return updated, audits


def remote_copy_text_to(text: str, destination: str) -> bool:
    with tempfile.TemporaryDirectory(prefix="tl-backfill-") as temp_dir:
        path = Path(temp_dir) / "record.json"
        path.write_text(text, encoding="utf-8")
        return run_rclone(["copyto", str(path), destination, "--log-level", "ERROR", "--stats", "0"]).returncode == 0


def process_remote(remote: str, source: str, process_log: str, legacy_backup: str, max_packages: int, apply: bool) -> tuple[int, list[dict[str, Any]]] | None:
    package_ids = list_remote_packages(remote, source, max_packages)
    if package_ids is None:
        return None

    audits: list[dict[str, Any]] = []
    updated = 0
    for package_id in package_ids:
        package_remote = join_remote(remote, f"{source.strip('/')}/{package_id}")
        entries = remote_entries(package_remote)
        if entries is None:
            audits.append({"state": "blocked", "reason": "package_list_failed", "kind": None})
            continue
        filenames = {
            str(item.get("Name"))
            for item in entries
            if not item.get("IsDir") and isinstance(item.get("Name"), str)
        }
        process_text = remote_text(f"{package_remote}/process-record.json")
        ingest = remote_json(f"{package_remote}/ingest.json")
        audit = package_audit(package_id=package_id, filenames=filenames, process_text=process_text, ingest=ingest)
        audits.append(audit)
        if not apply or audit["state"] != "backfillable" or ingest is None:
            continue

        if process_text is not None:
            backup_destination = join_remote(remote, f"{legacy_backup.strip('/')}/{package_id}-process-record.json")
            if not remote_copy_text_to(process_text, backup_destination):
                return None

        evidence = remote_json(f"{package_remote}/evidence-summary.json")
        record = build_backfill_record(package_id=package_id, kind=audit["kind"], ingest=ingest, evidence=evidence)
        serialized = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if not remote_copy_text_to(serialized, f"{package_remote}/process-record.json"):
            return None
        central_destination = join_remote(remote, f"{process_log.strip('/')}/{package_id}.json")
        if not remote_copy_text_to(serialized, central_destination):
            return None
        updated += 1

    return updated, audits


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit/backfill legacy READY process metadata without media reprocessing.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root-dir", type=Path)
    mode.add_argument("--remote")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--process-log", default=DEFAULT_PROCESS_LOG)
    parser.add_argument("--legacy-backup", default=DEFAULT_LEGACY_BACKUP)
    parser.add_argument("--max-packages", type=int, default=DEFAULT_MAX_PACKAGES)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.max_packages <= MAX_MAX_PACKAGES:
        parser.error(f"--max-packages must be between 1 and {MAX_MAX_PACKAGES}")
    if args.remote and not shutil.which("rclone"):
        print("legacy_ready_backfill_error code=rclone_missing")
        return 2

    if args.root_dir is not None:
        root = args.root_dir.resolve()
        process_log = root.parent / "PROCESS_LOG" / "RECORDS"
        legacy_backup = root.parent / "PROCESS_LOG" / "LEGACY_BACKFILL"
        updated, audits = process_local(root, process_log, legacy_backup, args.max_packages, args.apply)
    else:
        result = process_remote(args.remote, args.source, args.process_log, args.legacy_backup, args.max_packages, args.apply)
        if result is None:
            print("legacy_ready_backfill_error code=remote_operation_failed")
            return 2
        updated, audits = result

    summary = summarize(audits)
    mode_name = "apply" if args.apply else "dry_run"
    print(f"legacy_ready_backfill_ok mode={mode_name} updated={updated} {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
