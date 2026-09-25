#!/usr/bin/env python3
"""Validate isolated candidate publication without writing canonical knowledge.

This is the final read-only gate before any future candidate publisher is allowed
to touch 00_LIBRARY. It consumes the aggregated candidate outbox, validates the
canonical-shaped Markdown byte-for-byte, re-checks the current MASTER_INDEX and
checks target ownership in the live library.

V1 intentionally permits only:
- CREATE when the target does not exist and the current index has no collision;
- UNCHANGED when the exact bytes are already present under the same RECORD_ID.

A same-owner content change is blocked. Updating an existing canonical record
must use the explicit VALIDATED_UPDATE merge path, not smuggle an update through
a nominally new candidate. This program has no --apply mode and never writes
00_LIBRARY.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from candidate_curation_prepare import master_records, normalize_title
from candidate_publish_index import parse_candidate_manifest
from library_publish import derive_target, target_owner, validate_content

SCHEMA_VERSION = 1
VALIDATE_VERSION = "0.1.0"
DEFAULT_MANIFEST = "99_INBOX/CANDIDATES/PUBLISH_READY/latest.json"
DEFAULT_MASTER = "00_LIBRARY/MASTER_INDEX.md"
MAX_PLAN_BYTES = 128 * 1024


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["rclone", *arguments], check=False, capture_output=True)


def read_json_bytes(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read_local(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def read_remote(remote: str, relative: str) -> bytes | None:
    result = run_rclone(["cat", join_remote(remote, relative), "--log-level", "ERROR"])
    return result.stdout if result.returncode == 0 else None


def current_index_maps(master_text: str) -> tuple[dict[str, dict[str, str]], dict[str, list[dict[str, str]]]]:
    by_path: dict[str, dict[str, str]] = {}
    by_title: dict[str, list[dict[str, str]]] = {}
    for record in master_records(master_text):
        by_path[record["path"]] = record
        by_title.setdefault(record["title_key"], []).append(record)
    return by_path, by_title


def assess_publication(
    items: list[dict[str, str]],
    contents: dict[str, bytes | None],
    existing: dict[str, bytes | None],
    master_text: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    planned: list[dict[str, str]] = []
    by_path, by_title = current_index_maps(master_text)

    for index, raw_item in enumerate(items):
        item = dict(raw_item)
        target = derive_target(
            record_type=item["record_type"],
            domain=item["domain"],
            category=item["category"],
            slug=item["slug"],
        )
        item["target_path"] = target
        content = contents.get(item["content_path"])
        if content is None:
            errors.append(f"item_{index}_content_missing")
            continue
        try:
            validate_content(item, content)
        except ValueError as exc:
            errors.append(f"item_{index}_{exc}")
            continue

        title_key = normalize_title(item["title"])
        indexed_title_matches = by_title.get(title_key, [])
        if any(record["path"] != target for record in indexed_title_matches):
            errors.append(f"item_{index}_master_title_collision")
            continue
        indexed_at_target = by_path.get(target)
        if indexed_at_target is not None and indexed_at_target["title_key"] != title_key:
            errors.append(f"item_{index}_master_target_collision")
            continue

        current = existing.get(target)
        if current is None:
            if indexed_at_target is not None:
                errors.append(f"item_{index}_indexed_target_missing")
                continue
            action = "CREATE"
        elif current == content:
            owner = target_owner(current)
            if owner != item["record_id"]:
                errors.append(f"item_{index}_identical_target_owner_invalid")
                continue
            action = "UNCHANGED"
        else:
            owner = target_owner(current)
            if owner == item["record_id"]:
                errors.append(f"item_{index}_same_owner_change_requires_validated_update")
            else:
                errors.append(f"item_{index}_target_collision")
            continue

        planned.append(
            {
                "record_id": item["record_id"],
                "record_type": item["record_type"],
                "status": item["status"],
                "title": item["title"],
                "target_path": target,
                "content_sha256": item["content_sha256"],
                "action": action,
            }
        )

    if errors:
        return None, sorted(set(errors))
    plan = {
        "schema_version": SCHEMA_VERSION,
        "validate_version": VALIDATE_VERSION,
        "canonical_write_performed": False,
        "items": sorted(planned, key=lambda row: (row["target_path"], row["record_id"])),
        "counts": {
            "create": sum(1 for row in planned if row["action"] == "CREATE"),
            "unchanged": sum(1 for row in planned if row["action"] == "UNCHANGED"),
        },
    }
    encoded = json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PLAN_BYTES:
        return None, ["plan_too_large"]
    return plan, []


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run validate isolated candidate publication.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root-dir", type=Path)
    mode.add_argument("--remote")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--master", default=DEFAULT_MASTER)
    parser.add_argument("--plan-out", type=Path)
    args = parser.parse_args()

    if args.root_dir is not None:
        root = args.root_dir.resolve()
        manifest_raw = read_local(root / args.manifest)
        master_raw = read_local(root / args.master)
    else:
        if not shutil.which("rclone"):
            print("candidate_publish_validate_error code=rclone_missing canonical_write=0")
            return 2
        root = None
        manifest_raw = read_remote(str(args.remote), args.manifest)
        master_raw = read_remote(str(args.remote), args.master)

    payload = read_json_bytes(manifest_raw)
    if payload is None:
        print("candidate_publish_validate_error code=manifest_invalid_or_missing canonical_write=0")
        return 2
    if master_raw is None:
        print("candidate_publish_validate_error code=master_missing canonical_write=0")
        return 2
    try:
        master_text = master_raw.decode("utf-8")
    except UnicodeDecodeError:
        print("candidate_publish_validate_error code=master_utf8 canonical_write=0")
        return 2
    try:
        items = parse_candidate_manifest(payload)
    except ValueError as exc:
        print(f"candidate_publish_validate_error code={exc} canonical_write=0")
        return 2

    contents: dict[str, bytes | None] = {}
    existing: dict[str, bytes | None] = {}
    for item in items:
        target = derive_target(
            record_type=item["record_type"],
            domain=item["domain"],
            category=item["category"],
            slug=item["slug"],
        )
        if root is not None:
            contents[item["content_path"]] = read_local(root / item["content_path"])
            existing[target] = read_local(root / target)
        else:
            contents[item["content_path"]] = read_remote(str(args.remote), item["content_path"])
            existing[target] = read_remote(str(args.remote), target)

    plan, errors = assess_publication(items, contents, existing, master_text)
    if errors or plan is None:
        print(f"candidate_publish_validate_error codes={','.join(errors or ['validation_failed'])} canonical_write=0")
        return 2
    if args.plan_out:
        args.plan_out.parent.mkdir(parents=True, exist_ok=True)
        args.plan_out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "candidate_publish_validate_ok "
        f"items={len(plan['items'])} create={plan['counts']['create']} "
        f"unchanged={plan['counts']['unchanged']} canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
