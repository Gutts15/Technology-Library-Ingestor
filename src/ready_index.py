#!/usr/bin/env python3
"""Build a compact privacy-safe index for READY_FOR_ANALYSIS packages.

The index is an operational routing aid for later semantic curation. It does not
copy evidence text, filenames, provider file IDs, source names, URLs, OCR,
transcripts or document contents. It only exposes a package hash, derived kind,
processing state, completeness and generic relative pointers needed for
selective retrieval.
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

from package_contracts import required_package_files

INDEX_VERSION = "0.1.1"
SCHEMA_VERSION = 1
MAX_INDEX_BYTES = 96 * 1024
DEFAULT_SOURCE = "99_INBOX/READY_FOR_ANALYSIS"
DEFAULT_DESTINATION = "99_INBOX/CURATION/READY_INDEX/latest.json"
DEFAULT_MAX_PACKAGES = 100
MAX_MAX_PACKAGES = 500
PACKAGE_ID_RE = re.compile(r"^[0-9a-f]{20}$")

QUEUE_KIND = {
    "99_INBOX/TO_REVIEW/VIDEOS": "video",
    "99_INBOX/TO_REVIEW/IMAGES": "image",
    "99_INBOX/TO_REVIEW/AUDIO": "audio",
    "99_INBOX/TO_REVIEW/TEXT": "text",
    "99_INBOX/TO_REVIEW/DOCUMENTS": "document",
    "99_INBOX/TO_REVIEW/SPREADSHEETS": "spreadsheet",
    "99_INBOX/TO_REVIEW/LINKS": "link",
}
KINDS = ("video", "image", "audio", "text", "document", "spreadsheet", "link", "unknown")
DETAIL_ARTIFACT = {
    "video": "timeline.md",
    "image": "image-index.json",
    "audio": "audio-index.json",
    "text": "content-index.json",
    "document": "content-index.json",
    "spreadsheet": "spreadsheet-index.json",
    "link": "link-index.json",
}
PREVIEW_ARTIFACT = {
    "video": "contact-sheet.jpg",
    "image": "preview.jpg",
    "audio": "preview.opus",
}
SAFE_PROCESSING_STATUS = {"processed", "error"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


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


def safe_version(value: Any) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 48:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", value):
        return None
    return value


def read_json_text(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return read_json_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None


def sanitize_record(record: dict[str, Any], package_id: str) -> dict[str, Any] | None:
    if record.get("schema_version") != 1:
        return None
    if record.get("package_id") != package_id:
        return None

    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    processing = record.get("processing") if isinstance(record.get("processing"), dict) else {}
    source_folder = source.get("source_folder") if isinstance(source.get("source_folder"), str) else ""
    source_folder = source_folder.strip("/")
    kind = QUEUE_KIND.get(source_folder, "unknown")

    status = processing.get("status")
    if status not in SAFE_PROCESSING_STATUS:
        status = "unknown"

    processed_at = parse_iso(processing.get("processed_at"))
    pipeline_version = safe_version(processing.get("pipeline_version"))
    compactor_version = safe_version(processing.get("compactor_version"))

    return {
        "kind": kind,
        "source_folder": source_folder,
        "processing_status": status,
        "processed_at": processed_at,
        "pipeline_version": pipeline_version,
        "compactor_version": compactor_version,
    }


def generic_pointer(source: str, package_id: str, filename: str) -> str:
    return f"{source.strip('/')}/{package_id}/{filename}"


def build_item(
    *,
    package_id: str,
    record: dict[str, Any],
    filenames: set[str],
    source: str,
    evidence_bytes: int,
) -> dict[str, Any] | None:
    meta = sanitize_record(record, package_id)
    if meta is None:
        return None

    required = required_package_files(meta["source_folder"])
    complete = required is not None and all(name in filenames for name in required)
    kind = meta["kind"]
    detail_name = DETAIL_ARTIFACT.get(kind)
    preview_name = PREVIEW_ARTIFACT.get(kind)

    return {
        "package_id": package_id,
        "kind": kind,
        "processing_status": meta["processing_status"],
        "complete": complete,
        "eligible_for_analysis": bool(complete),
        "processed_at": meta["processed_at"].isoformat() if meta["processed_at"] else None,
        "pipeline_version": meta["pipeline_version"],
        "compactor_version": meta["compactor_version"],
        "evidence_bytes": safe_int(evidence_bytes),
        "evidence_path": generic_pointer(source, package_id, "evidence-summary.json") if "evidence-summary.json" in filenames else None,
        "detail_path": generic_pointer(source, package_id, detail_name) if detail_name and detail_name in filenames else None,
        "preview_path": generic_pointer(source, package_id, preview_name) if preview_name and preview_name in filenames else None,
    }


def list_local_package(root: Path, package_id: str, source: str) -> dict[str, Any] | None:
    package = root / package_id
    record = read_json_file(package / "process-record.json")
    if record is None:
        return None
    filenames = {path.name for path in package.iterdir() if path.is_file()}
    evidence = package / "evidence-summary.json"
    evidence_bytes = evidence.stat().st_size if evidence.is_file() else 0
    return build_item(
        package_id=package_id,
        record=record,
        filenames=filenames,
        source=source,
        evidence_bytes=evidence_bytes,
    )


def load_local(root: Path, source: str, max_packages: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    entries = [path for path in root.iterdir()] if root.is_dir() else []
    valid_dirs = [path for path in entries if path.is_dir() and PACKAGE_ID_RE.fullmatch(path.name)]
    invalid_dirs = sum(1 for path in entries if path.is_dir() and not PACKAGE_ID_RE.fullmatch(path.name))
    valid_dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    selected = valid_dirs[:max_packages]

    items: list[dict[str, Any]] = []
    invalid_records = 0
    for path in selected:
        item = list_local_package(root, path.name, source)
        if item is None:
            invalid_records += 1
            continue
        items.append(item)
    return items, {
        "packages_seen": len(valid_dirs),
        "packages_selected": len(selected),
        "invalid_directories": invalid_dirs,
        "invalid_records": invalid_records,
    }


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def remote_json(remote_path: str) -> dict[str, Any] | None:
    result = run_rclone(["cat", remote_path, "--log-level", "ERROR"])
    if result.returncode != 0:
        return None
    return read_json_text(result.stdout)


def remote_dir_entries(remote_path: str) -> list[dict[str, Any]] | None:
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


def list_remote_package(remote: str, source: str, package_id: str) -> dict[str, Any] | None:
    package_remote = join_remote(remote, f"{source.strip('/')}/{package_id}")
    entries = remote_dir_entries(package_remote)
    if entries is None:
        return None
    files = {
        str(item.get("Name")): item
        for item in entries
        if not item.get("IsDir") and isinstance(item.get("Name"), str)
    }
    record = remote_json(f"{package_remote}/process-record.json")
    if record is None:
        return None
    evidence_entry = files.get("evidence-summary.json") or {}
    evidence_bytes = safe_int(evidence_entry.get("Size")) if isinstance(evidence_entry, dict) else 0
    return build_item(
        package_id=package_id,
        record=record,
        filenames=set(files),
        source=source,
        evidence_bytes=evidence_bytes,
    )


def load_remote(remote: str, source: str, max_packages: int) -> tuple[list[dict[str, Any]], dict[str, int]] | None:
    if not shutil.which("rclone"):
        return None
    root_entries = remote_dir_entries(join_remote(remote, source))
    if root_entries is None:
        return None

    valid: list[tuple[datetime, str]] = []
    invalid_dirs = 0
    for item in root_entries:
        if not item.get("IsDir"):
            continue
        name = item.get("Name")
        if not isinstance(name, str) or not PACKAGE_ID_RE.fullmatch(name):
            invalid_dirs += 1
            continue
        mod_time = parse_iso(item.get("ModTime")) or datetime.fromtimestamp(0, tz=timezone.utc)
        valid.append((mod_time, name))

    valid.sort(key=lambda item: item[0], reverse=True)
    selected = valid[:max_packages]
    items: list[dict[str, Any]] = []
    invalid_records = 0
    for _, package_id in selected:
        item = list_remote_package(remote, source, package_id)
        if item is None:
            invalid_records += 1
            continue
        items.append(item)
    return items, {
        "packages_seen": len(valid),
        "packages_selected": len(selected),
        "invalid_directories": invalid_dirs,
        "invalid_records": invalid_records,
    }


def sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(item: dict[str, Any]) -> tuple[int, str]:
        parsed = parse_iso(item.get("processed_at"))
        stamp = int(parsed.timestamp()) if parsed else 0
        return stamp, str(item.get("package_id") or "")

    return sorted(items, key=key, reverse=True)


def build_index(items: list[dict[str, Any]], stats: dict[str, int], max_packages: int, source: str) -> dict[str, Any]:
    ordered = sort_items(items)
    by_kind = {kind: 0 for kind in KINDS}
    complete = 0
    eligible = 0
    for item in ordered:
        kind = item.get("kind") if item.get("kind") in by_kind else "unknown"
        by_kind[kind] += 1
        complete += int(bool(item.get("complete")))
        eligible += int(bool(item.get("eligible_for_analysis")))

    return {
        "schema_version": SCHEMA_VERSION,
        "index_version": INDEX_VERSION,
        "generated_at": utc_now(),
        "source": source.strip("/"),
        "window": {
            **stats,
            "packages_indexed": len(ordered),
            "complete_packages": complete,
            "eligible_packages": eligible,
            "max_packages": max_packages,
        },
        "by_kind": by_kind,
        "items": ordered,
    }


def encoded_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def persist_remote(remote: str, destination: str, local_path: Path) -> bool:
    parent = destination.rsplit("/", 1)[0] if "/" in destination else ""
    if parent:
        if run_rclone(["mkdir", join_remote(remote, parent), "--log-level", "ERROR"]).returncode != 0:
            return False
    return run_rclone([
        "copyto", str(local_path), join_remote(remote, destination),
        "--log-level", "ERROR", "--stats", "0",
    ]).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a privacy-safe selective-retrieval index for READY_FOR_ANALYSIS.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root-dir", type=Path)
    mode.add_argument("--remote")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--destination", default=DEFAULT_DESTINATION)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-packages", type=int, default=DEFAULT_MAX_PACKAGES)
    args = parser.parse_args()

    if not 1 <= args.max_packages <= MAX_MAX_PACKAGES:
        parser.error(f"--max-packages must be between 1 and {MAX_MAX_PACKAGES}")

    if args.root_dir is not None:
        items, stats = load_local(args.root_dir.resolve(), args.source, args.max_packages)
    else:
        loaded = load_remote(args.remote, args.source, args.max_packages)
        if loaded is None:
            print("ready_index_error code=remote_read_failed")
            return 2
        items, stats = loaded

    payload = build_index(items, stats, args.max_packages, args.source)
    if encoded_size(payload) > MAX_INDEX_BYTES:
        print("ready_index_error code=index_budget_exceeded")
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    uploaded = False
    if args.remote:
        uploaded = persist_remote(args.remote, args.destination, args.out)
        if not uploaded:
            print("ready_index_warning code=upload_failed")
            return 2

    print(
        "ready_index_ok "
        f"seen={payload['window']['packages_seen']} indexed={payload['window']['packages_indexed']} "
        f"eligible={payload['window']['eligible_packages']} invalid_records={payload['window']['invalid_records']} "
        f"uploaded={1 if uploaded else 0}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
