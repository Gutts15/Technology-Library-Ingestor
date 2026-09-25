#!/usr/bin/env python3
"""Validate and index private Technology Library chat-research candidates.

This stage is intentionally mechanical. It never promotes candidate content into
00_LIBRARY and never performs paid API calls. Stdout contains aggregate counts
only; candidate titles, filenames, URLs and body text remain in private storage.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

QUEUE_VERSION = "0.1.0"
SCHEMA_VERSION = 1
DEFAULT_ROOT = "99_INBOX/CANDIDATES"
DEFAULT_CHAT_RESEARCH = "CHAT_RESEARCH"
DEFAULT_INDEX_NAME = "index.json"
MAX_CANDIDATE_BYTES = 64 * 1024
MAX_CANDIDATES = 500

REQUIRED_HEADERS = (
    "TYPE",
    "CANDIDATE_STATUS",
    "TITLE",
    "LAST_CHECKED",
    "SOURCE_ORIGIN",
)
ALLOWED_CANDIDATE_STATUS = {"TO_REVIEW"}
ALLOWED_PROPOSED_TYPE = {"TECHNOLOGY", "PATTERN", "PIPELINE", "SOURCE"}
ALLOWED_PROPOSED_STATUS = {"REFERENCE", "TEST", "DEPRECATED"}
HEADER_RE = re.compile(r"^([A-Z][A-Z0-9_]{1,63}):\s*(.*)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAFE_NAME_RE = re.compile(r"^candidate-[A-Za-z0-9._-]+\.md$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )


def ensure_remote_dir(remote: str, relative: str) -> bool:
    return run_rclone([
        "mkdir", join_remote(remote, relative), "--log-level", "ERROR"
    ]).returncode == 0


def list_candidate_items(remote: str, relative: str) -> list[dict[str, Any]] | None:
    result = run_rclone([
        "lsjson",
        join_remote(remote, relative),
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
    items.sort(key=lambda item: str(item.get("Path") or item.get("Name") or ""))
    return items[:MAX_CANDIDATES]


def item_name(item: dict[str, Any]) -> str | None:
    raw = item.get("Name") or item.get("Path")
    if not isinstance(raw, str) or not raw:
        return None
    return PurePosixPath(raw).name


def item_size(item: dict[str, Any]) -> int | None:
    raw = item.get("Size")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def read_remote_text(remote: str, relative: str) -> str | None:
    result = run_rclone([
        "cat", join_remote(remote, relative), "--log-level", "ERROR"
    ])
    if result.returncode != 0:
        return None
    return result.stdout


def parse_headers(text: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            break
        match = HEADER_RE.fullmatch(line.rstrip())
        if not match:
            break
        key, value = match.groups()
        headers[key] = value.strip()
    return headers


def valid_date(value: str) -> bool:
    if not DATE_RE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def validate_candidate(name: str, text: str) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    headers = parse_headers(text)

    if not SAFE_NAME_RE.fullmatch(name):
        errors.append("invalid_filename")

    missing = [key for key in REQUIRED_HEADERS if not headers.get(key)]
    if missing:
        errors.append("missing_required_headers")

    if headers.get("TYPE") != "CANDIDATE":
        errors.append("invalid_type")

    candidate_status = headers.get("CANDIDATE_STATUS")
    if candidate_status and candidate_status not in ALLOWED_CANDIDATE_STATUS:
        errors.append("invalid_candidate_status")

    proposed_type = headers.get("PROPOSED_TYPE")
    if proposed_type and proposed_type not in ALLOWED_PROPOSED_TYPE:
        errors.append("invalid_proposed_type")

    proposed_status = headers.get("PROPOSED_STATUS")
    if proposed_status and proposed_status not in ALLOWED_PROPOSED_STATUS:
        errors.append("invalid_proposed_status")

    last_checked = headers.get("LAST_CHECKED")
    if last_checked and not valid_date(last_checked):
        errors.append("invalid_last_checked")

    title = headers.get("TITLE", "")
    if len(title) > 200:
        errors.append("title_too_long")

    origin = headers.get("SOURCE_ORIGIN", "")
    if len(origin) > 1000:
        errors.append("source_origin_too_long")

    return headers, sorted(set(errors))


def candidate_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def build_index(remote: str, root: str, chat_subdir: str) -> tuple[dict[str, Any] | None, str | None]:
    chat_relative = str(PurePosixPath(root) / chat_subdir)
    if not ensure_remote_dir(remote, chat_relative):
        return None, "queue_unavailable"

    items = list_candidate_items(remote, chat_relative)
    if items is None:
        return None, "queue_list_failed"

    entries: list[dict[str, Any]] = []
    seen_hashes: dict[str, int] = {}

    for item in items:
        name = item_name(item)
        if not name:
            entries.append({"valid": False, "errors": ["missing_filename"]})
            continue

        size = item_size(item)
        if size is not None and size > MAX_CANDIDATE_BYTES:
            entries.append({
                "path": str(PurePosixPath(chat_subdir) / name),
                "valid": False,
                "errors": ["candidate_too_large"],
            })
            continue

        relative = str(PurePosixPath(chat_relative) / name)
        text = read_remote_text(remote, relative)
        if text is None:
            entries.append({
                "path": str(PurePosixPath(chat_subdir) / name),
                "valid": False,
                "errors": ["candidate_read_failed"],
            })
            continue

        encoded_size = len(text.encode("utf-8"))
        if encoded_size > MAX_CANDIDATE_BYTES:
            entries.append({
                "path": str(PurePosixPath(chat_subdir) / name),
                "valid": False,
                "errors": ["candidate_too_large"],
            })
            continue

        headers, errors = validate_candidate(name, text)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        seen_hashes[digest] = seen_hashes.get(digest, 0) + 1

        entry: dict[str, Any] = {
            "candidate_id": digest[:20],
            "path": str(PurePosixPath(chat_subdir) / name),
            "valid": not errors,
            "errors": errors,
            "candidate_status": headers.get("CANDIDATE_STATUS"),
            "proposed_type": headers.get("PROPOSED_TYPE"),
            "proposed_status": headers.get("PROPOSED_STATUS"),
            "proposed_domain": headers.get("PROPOSED_DOMAIN"),
            "proposed_category": headers.get("PROPOSED_CATEGORY"),
            "last_checked": headers.get("LAST_CHECKED"),
            "size_bytes": encoded_size,
            "sha256": digest,
        }
        entries.append(entry)

    exact_duplicate_instances = sum(max(0, count - 1) for count in seen_hashes.values())
    valid_count = sum(1 for entry in entries if entry.get("valid") is True)
    invalid_count = len(entries) - valid_count

    payload = {
        "schema_version": SCHEMA_VERSION,
        "queue_version": QUEUE_VERSION,
        "generated_at": utc_now(),
        "root": root,
        "chat_research_path": chat_relative,
        "total": len(entries),
        "valid": valid_count,
        "invalid": invalid_count,
        "exact_duplicate_instances": exact_duplicate_instances,
        "entries": entries,
    }
    return payload, None


def write_index(remote: str, root: str, index_name: str, payload: dict[str, Any]) -> bool:
    if not ensure_remote_dir(remote, root):
        return False

    with tempfile.TemporaryDirectory(prefix="tl-candidate-queue-") as temp_dir:
        path = Path(temp_dir) / index_name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result = run_rclone([
            "copyto",
            str(path),
            join_remote(remote, str(PurePosixPath(root) / index_name)),
            "--log-level",
            "ERROR",
            "--stats",
            "0",
        ])
        return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and index private chat-research candidate records."
    )
    parser.add_argument("--remote", default="tl:")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--chat-subdir", default=DEFAULT_CHAT_RESEARCH)
    parser.add_argument("--index-name", default=DEFAULT_INDEX_NAME)
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("candidate_queue_error code=rclone_missing")
        return 2

    payload, error = build_index(args.remote, args.root, args.chat_subdir)
    if payload is None:
        print(f"candidate_queue_error code={error or 'build_failed'}")
        return 2

    if not write_index(args.remote, args.root, args.index_name, payload):
        print("candidate_queue_error code=index_write_failed")
        return 2

    print(
        "candidate_queue_ok "
        f"total={payload['total']} valid={payload['valid']} "
        f"invalid={payload['invalid']} exact_duplicates={payload['exact_duplicate_instances']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
