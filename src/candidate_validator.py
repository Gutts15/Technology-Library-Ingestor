#!/usr/bin/env python3
"""Deterministic preflight for Technology Library candidates.

This validator intentionally does not perform semantic truth validation or web
research. It provides the zero-cost mechanical layer that can safely detect
schema problems, exact duplicate candidate content and exact normalized-title
collisions with current canonical records.

It never writes to 00_LIBRARY. Output is a private validation proposal file for
a later semantic validator / curator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_queue import (
    DEFAULT_CHAT_RESEARCH,
    DEFAULT_ROOT,
    MAX_CANDIDATE_BYTES,
    item_name,
    item_size,
    join_remote,
    list_candidate_items,
    parse_headers,
    read_remote_text,
    validate_candidate,
)

VALIDATOR_VERSION = "0.1.0"
SCHEMA_VERSION = 1
DEFAULT_LIBRARY_ROOT = "00_LIBRARY"
DEFAULT_MASTER_INDEX = "MASTER_INDEX.md"
DEFAULT_OUTPUT_NAME = "validation-proposals.json"

CANONICAL_LINE_RE = re.compile(
    r"^-\s+(TECHNOLOGY|PATTERN|PIPELINE|SOURCE)\s+-\s+(.+?)\s+"
    r"\[(REFERENCE|TEST|DEPRECATED)\]\s+\(`([^`]+)`\)\s*$"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *arguments], check=False, capture_output=True, text=True
    )


def normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    asciiish = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    asciiish = asciiish.casefold()
    asciiish = re.sub(r"[^a-z0-9]+", " ", asciiish)
    return " ".join(asciiish.split())


def parse_master_index(text: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for raw in text.splitlines():
        match = CANONICAL_LINE_RE.fullmatch(raw.strip())
        if not match:
            continue
        record_type, title, status, path = match.groups()
        records.append({
            "type": record_type,
            "title": title,
            "title_key": normalize_title(title),
            "status": status,
            "path": path,
        })
    return records


def read_master_index(remote: str, library_root: str, master_index: str) -> str | None:
    relative = str(PurePosixPath(library_root) / master_index)
    result = run_rclone([
        "cat", join_remote(remote, relative), "--log-level", "ERROR"
    ])
    if result.returncode != 0:
        return None
    return result.stdout


def build_proposals(
    remote: str,
    candidate_root: str,
    chat_subdir: str,
    library_root: str,
    master_index: str,
) -> tuple[dict[str, Any] | None, str | None]:
    master_text = read_master_index(remote, library_root, master_index)
    if master_text is None:
        return None, "master_index_read_failed"
    canonical = parse_master_index(master_text)
    canonical_by_title: dict[str, list[dict[str, str]]] = {}
    for record in canonical:
        canonical_by_title.setdefault(record["title_key"], []).append(record)

    chat_relative = str(PurePosixPath(candidate_root) / chat_subdir)
    items = list_candidate_items(remote, chat_relative)
    if items is None:
        return None, "candidate_queue_list_failed"

    entries: list[dict[str, Any]] = []
    seen_hashes: dict[str, str] = {}

    for item in items:
        name = item_name(item)
        if not name:
            entries.append({
                "candidate_id": None,
                "proposal": "NEEDS_REVIEW",
                "reasons": ["missing_filename"],
            })
            continue

        size = item_size(item)
        relative_path = str(PurePosixPath(chat_subdir) / name)
        if size is not None and size > MAX_CANDIDATE_BYTES:
            entries.append({
                "candidate_id": None,
                "path": relative_path,
                "proposal": "NEEDS_REVIEW",
                "reasons": ["candidate_too_large"],
            })
            continue

        source_relative = str(PurePosixPath(chat_relative) / name)
        text = read_remote_text(remote, source_relative)
        if text is None:
            entries.append({
                "candidate_id": None,
                "path": relative_path,
                "proposal": "NEEDS_REVIEW",
                "reasons": ["candidate_read_failed"],
            })
            continue

        if len(text.encode("utf-8")) > MAX_CANDIDATE_BYTES:
            entries.append({
                "candidate_id": None,
                "path": relative_path,
                "proposal": "NEEDS_REVIEW",
                "reasons": ["candidate_too_large"],
            })
            continue

        headers, schema_errors = validate_candidate(name, text)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        candidate_id = digest[:20]
        title = headers.get("TITLE", "")
        title_key = normalize_title(title)
        canonical_matches = canonical_by_title.get(title_key, []) if title_key else []

        if schema_errors:
            proposal = "NEEDS_REVIEW"
            reasons = ["schema_invalid", *schema_errors]
        elif digest in seen_hashes:
            proposal = "DUPLICATE"
            reasons = ["exact_candidate_duplicate"]
        elif canonical_matches:
            proposal = "NEEDS_REVIEW"
            reasons = ["canonical_title_match"]
        else:
            proposal = "READY_FOR_SEMANTIC"
            reasons = []

        entry: dict[str, Any] = {
            "candidate_id": candidate_id,
            "path": relative_path,
            "proposal": proposal,
            "reasons": reasons,
            "candidate_status": headers.get("CANDIDATE_STATUS"),
            "proposed_type": headers.get("PROPOSED_TYPE"),
            "proposed_status": headers.get("PROPOSED_STATUS"),
            "proposed_domain": headers.get("PROPOSED_DOMAIN"),
            "proposed_category": headers.get("PROPOSED_CATEGORY"),
            "last_checked": headers.get("LAST_CHECKED"),
            "sha256": digest,
        }
        if canonical_matches:
            entry["canonical_matches"] = [
                {
                    "type": record["type"],
                    "status": record["status"],
                    "path": record["path"],
                }
                for record in canonical_matches
            ]

        entries.append(entry)
        seen_hashes.setdefault(digest, candidate_id)

    counts: dict[str, int] = {}
    for entry in entries:
        key = str(entry.get("proposal") or "UNKNOWN")
        counts[key] = counts.get(key, 0) + 1

    payload = {
        "schema_version": SCHEMA_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "generated_at": utc_now(),
        "candidate_root": candidate_root,
        "canonical_records_seen": len(canonical),
        "counts": counts,
        "entries": entries,
        "semantic_validation_complete": False,
        "canonical_write_performed": False,
    }
    return payload, None


def write_proposals(remote: str, root: str, output_name: str, payload: dict[str, Any]) -> bool:
    with tempfile.TemporaryDirectory(prefix="tl-candidate-validator-") as temp_dir:
        path = Path(temp_dir) / output_name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result = run_rclone([
            "copyto",
            str(path),
            join_remote(remote, str(PurePosixPath(root) / output_name)),
            "--log-level",
            "ERROR",
            "--stats",
            "0",
        ])
        return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build zero-cost deterministic validation proposals for candidates."
    )
    parser.add_argument("--remote", default="tl:")
    parser.add_argument("--candidate-root", default=DEFAULT_ROOT)
    parser.add_argument("--chat-subdir", default=DEFAULT_CHAT_RESEARCH)
    parser.add_argument("--library-root", default=DEFAULT_LIBRARY_ROOT)
    parser.add_argument("--master-index", default=DEFAULT_MASTER_INDEX)
    parser.add_argument("--output-name", default=DEFAULT_OUTPUT_NAME)
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("candidate_validator_error code=rclone_missing")
        return 2

    payload, error = build_proposals(
        args.remote,
        args.candidate_root,
        args.chat_subdir,
        args.library_root,
        args.master_index,
    )
    if payload is None:
        print(f"candidate_validator_error code={error or 'build_failed'}")
        return 2

    if not write_proposals(args.remote, args.candidate_root, args.output_name, payload):
        print("candidate_validator_error code=proposal_write_failed")
        return 2

    counts = payload["counts"]
    print(
        "candidate_validator_ok "
        f"total={len(payload['entries'])} "
        f"ready={counts.get('READY_FOR_SEMANTIC', 0)} "
        f"duplicate={counts.get('DUPLICATE', 0)} "
        f"review={counts.get('NEEDS_REVIEW', 0)} canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
