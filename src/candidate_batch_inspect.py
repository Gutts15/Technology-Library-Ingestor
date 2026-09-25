#!/usr/bin/env python3
"""Inspect one sealed candidate finalization batch without publishing anything.

The local finalizer deliberately produces several private control files and record
artifacts. This module turns that state into one bounded operator-review report.
It verifies the FINALIZE_BATCH byte bindings, validates the current NEW and UPDATE
selections, rechecks their live canonical preconditions, and exposes concise
record previews for editorial review.

The report is read-only with respect to storage. It never writes 00_LIBRARY and
never mutates candidate lifecycle state.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from candidate_finalize_batch import verify_finalize_batch
from candidate_publish_index import parse_candidate_manifest
from candidate_publish_validate import VALIDATE_VERSION
from candidate_transaction_prepare import parse_update_manifest
from library_index_build import validate_record
from library_publish import derive_target, validate_content

SCHEMA_VERSION = 1
INSPECT_VERSION = "0.1.0"
DEFAULT_ROOT = "99_INBOX/CANDIDATES"
DEFAULT_MASTER = "00_LIBRARY/MASTER_INDEX.md"
DEFAULT_PUBLISH = "PUBLISH_READY/latest.json"
DEFAULT_VALIDATION = "PUBLISH_READY/dry-run-plan.json"
DEFAULT_UPDATE = "UPDATE_READY/latest.json"
DEFAULT_BATCH = "FINALIZE_BATCH/current.json"
MAX_ITEMS = 50
PREVIEW_CHARS = 1400
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["rclone", *arguments], check=False, capture_output=True)


def read_local(root: Path, relative: str) -> bytes | None:
    try:
        return (root / relative).read_bytes()
    except OSError:
        return None


def read_remote(remote: str, relative: str) -> bytes | None:
    result = run_rclone(["cat", join_remote(remote, relative), "--log-level", "ERROR"])
    return result.stdout if result.returncode == 0 else None


def read_json_bytes(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def parse_validation(payload: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(payload, dict):
        return [], ["publish_validation_missing"]
    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("publish_validation_schema")
    if payload.get("validate_version") != VALIDATE_VERSION:
        errors.append("publish_validation_version")
    if payload.get("canonical_write_performed") is not False:
        errors.append("publish_validation_write_flag")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        return [], sorted(set(errors + ["publish_validation_items"]))

    output: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            errors.append(f"publish_validation_{index}_invalid")
            continue
        target = raw.get("target_path")
        action = raw.get("action")
        record_id = raw.get("record_id")
        content_sha = raw.get("content_sha256")
        if not isinstance(target, str) or not target.startswith("00_LIBRARY/"):
            errors.append(f"publish_validation_{index}_target")
        elif target in seen_targets:
            errors.append(f"publish_validation_{index}_duplicate_target")
        else:
            seen_targets.add(target)
        if action not in {"CREATE", "UNCHANGED"}:
            errors.append(f"publish_validation_{index}_action")
        if not isinstance(record_id, str) or len(record_id) != 20:
            errors.append(f"publish_validation_{index}_record_id")
        if not isinstance(content_sha, str) or not SHA_RE.fullmatch(content_sha):
            errors.append(f"publish_validation_{index}_content_sha")
        output.append(dict(raw))
    return output, sorted(set(errors))


def _preview(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return text[:PREVIEW_CHARS].rstrip()


def _master_count(master_raw: bytes) -> int | None:
    try:
        text = master_raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    match = re.search(r"^RECORDS:\s*(\d+)\s*$", text, flags=re.MULTILINE)
    return int(match.group(1)) if match else None


def inspect_batch(
    *,
    master_raw: bytes | None,
    publish_raw: bytes | None,
    validation_raw: bytes | None,
    update_raw: bytes | None,
    batch_raw: bytes | None,
    read_content: Callable[[str], bytes | None],
) -> tuple[dict[str, Any] | None, list[str]]:
    if master_raw is None:
        return None, ["master_missing"]
    if publish_raw is None:
        return None, ["publish_manifest_missing"]
    if validation_raw is None:
        return None, ["publish_validation_missing"]
    if update_raw is None:
        return None, ["update_manifest_missing"]

    publish = read_json_bytes(publish_raw)
    validation = read_json_bytes(validation_raw)
    update = read_json_bytes(update_raw)
    batch = read_json_bytes(batch_raw)
    if publish is None:
        return None, ["publish_manifest_invalid"]
    if validation is None:
        return None, ["publish_validation_invalid"]
    if update is None:
        return None, ["update_manifest_invalid"]
    if batch is None:
        return None, ["finalize_batch_invalid"]

    batch_errors = verify_finalize_batch(batch, master_raw, publish_raw, validation_raw, update_raw)
    if batch_errors:
        return None, batch_errors

    try:
        new_items = parse_candidate_manifest(publish)
    except ValueError as exc:
        return None, [f"publish_manifest_{exc}"]
    validation_items, validation_errors = parse_validation(validation)
    updates, update_errors = parse_update_manifest(update)
    errors = [*validation_errors, *update_errors]
    if errors:
        return None, sorted(set(errors))

    by_target: dict[str, dict[str, Any]] = {
        str(item.get("target_path")): item for item in validation_items
    }
    expected_targets = {
        derive_target(
            record_type=item["record_type"],
            domain=item["domain"],
            category=item["category"],
            slug=item["slug"],
        )
        for item in new_items
    }
    if set(by_target) != expected_targets:
        return None, ["publish_validation_item_set_mismatch"]

    rows: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    seen_records: set[str] = set()
    create_count = 0
    unchanged_count = 0
    update_count = 0

    for index, item in enumerate(new_items):
        target = derive_target(
            record_type=item["record_type"],
            domain=item["domain"],
            category=item["category"],
            slug=item["slug"],
        )
        validation_item = by_target[target]
        if validation_item.get("record_id") != item["record_id"]:
            errors.append(f"new_{index}_record_id_binding")
            continue
        if validation_item.get("content_sha256") != item["content_sha256"]:
            errors.append(f"new_{index}_content_sha_binding")
            continue
        action = validation_item.get("action")
        content = read_content(item["content_path"])
        if content is None:
            errors.append(f"new_{index}_content_missing")
            continue
        if sha256(content) != item["content_sha256"]:
            errors.append(f"new_{index}_content_sha_changed")
            continue
        try:
            validate_content(item, content)
            record = validate_record(target, content)
        except ValueError as exc:
            errors.append(f"new_{index}_content_{exc}")
            continue
        current = read_content(target)
        if action == "CREATE":
            if current is not None:
                errors.append(f"new_{index}_create_target_exists")
                continue
            create_count += 1
        elif action == "UNCHANGED":
            if current != content:
                errors.append(f"new_{index}_unchanged_target_changed")
                continue
            unchanged_count += 1
        else:
            errors.append(f"new_{index}_action")
            continue

        if target in seen_targets:
            errors.append(f"new_{index}_cross_lane_target_collision")
        if item["record_id"] in seen_records:
            errors.append(f"new_{index}_cross_lane_record_collision")
        seen_targets.add(target)
        seen_records.add(item["record_id"])
        rows.append(
            {
                "lane": "NEW",
                "action": action,
                "record_id": item["record_id"],
                "record_type": record["record_type"],
                "status": record["status"],
                "domain": record["domain"],
                "category": record["category"],
                "title": record["title"],
                "target_path": target,
                "content_path": item["content_path"],
                "content_sha256": item["content_sha256"],
                "package_id": item["package_id"],
                "revision_key": item["revision_key"],
                "preview": _preview(content),
            }
        )

    for index, artifact in enumerate(updates):
        target = artifact.get("target_path")
        content_path = artifact.get("content_path")
        merged_sha = artifact.get("merged_sha256")
        base_sha = artifact.get("base_sha256")
        record_id = artifact.get("record_id")
        if not all(isinstance(value, str) and value for value in (target, content_path, merged_sha, base_sha, record_id)):
            errors.append(f"update_{index}_fields")
            continue
        if not SHA_RE.fullmatch(str(merged_sha)) or not SHA_RE.fullmatch(str(base_sha)):
            errors.append(f"update_{index}_sha")
            continue
        content = read_content(str(content_path))
        if content is None:
            errors.append(f"update_{index}_content_missing")
            continue
        if sha256(content) != merged_sha:
            errors.append(f"update_{index}_content_sha_changed")
            continue
        try:
            record = validate_record(str(target), content)
        except ValueError as exc:
            errors.append(f"update_{index}_content_{exc}")
            continue
        if record["record_id"] != record_id:
            errors.append(f"update_{index}_record_id_binding")
            continue
        current = read_content(str(target))
        if current is None:
            errors.append(f"update_{index}_target_missing")
            continue
        if sha256(current) != base_sha:
            errors.append(f"update_{index}_base_sha_changed")
            continue

        if target in seen_targets:
            errors.append(f"update_{index}_cross_lane_target_collision")
        if record_id in seen_records:
            errors.append(f"update_{index}_cross_lane_record_collision")
        seen_targets.add(str(target))
        seen_records.add(str(record_id))
        update_count += 1
        rows.append(
            {
                "lane": "UPDATE",
                "action": "UPDATE",
                "record_id": record_id,
                "record_type": record["record_type"],
                "status": record["status"],
                "domain": record["domain"],
                "category": record["category"],
                "title": record["title"],
                "target_path": target,
                "content_path": content_path,
                "content_sha256": merged_sha,
                "candidate_id": artifact.get("candidate_id"),
                "base_sha256": base_sha,
                "claims_input": artifact.get("claims_input"),
                "claims_added": artifact.get("claims_added"),
                "claims_deduped": artifact.get("claims_deduped"),
                "preview": _preview(content),
            }
        )

    if errors:
        return None, sorted(set(errors))

    current_count = _master_count(master_raw)
    expected_after = current_count + create_count if current_count is not None else None
    ordered = sorted(rows, key=lambda row: (str(row["target_path"]), str(row["record_id"])))
    state = "EMPTY_BATCH" if not ordered else "READY_FOR_EDITORIAL_REVIEW"
    return {
        "schema_version": SCHEMA_VERSION,
        "inspect_version": INSPECT_VERSION,
        "state": state,
        "batch_id": batch.get("batch_id"),
        "master_index_sha256": sha256(master_raw),
        "current_library_record_count": current_count,
        "expected_library_record_count_after_commit": expected_after,
        "live_preconditions_verified": True,
        "canonical_write_performed": False,
        "counts": {
            "items": len(ordered),
            "new_lane": len(new_items),
            "update_lane": len(updates),
            "create": create_count,
            "unchanged": unchanged_count,
            "update": update_count,
        },
        "items": ordered,
    }, []


def render_markdown(report: dict[str, Any]) -> str:
    counts = report["counts"]
    lines = [
        "# Candidate Batch Inspection",
        "",
        f"STATE: {report['state']}",
        f"BATCH_ID: {report.get('batch_id')}",
        f"CURRENT_LIBRARY_RECORDS: {report.get('current_library_record_count')}",
        f"EXPECTED_AFTER_COMMIT: {report.get('expected_library_record_count_after_commit')}",
        "LIVE_PRECONDITIONS_VERIFIED: TRUE",
        "CANONICAL_WRITE_PERFORMED: FALSE",
        "",
        "## COUNTS",
        "",
        f"- Items: {counts['items']}",
        f"- CREATE: {counts['create']}",
        f"- UPDATE: {counts['update']}",
        f"- UNCHANGED: {counts['unchanged']}",
    ]
    for index, item in enumerate(report["items"], 1):
        lines.extend(
            [
                "",
                f"## {index}. {item['title']}",
                "",
                f"- Lane/action: {item['lane']} / {item['action']}",
                f"- Type/status: {item['record_type']} / {item['status']}",
                f"- Domain/category: {item['domain']} / {item['category']}",
                f"- Record ID: `{item['record_id']}`",
                f"- Target: `{item['target_path']}`",
                f"- Private content: `{item['content_path']}`",
                "",
                "### Preview",
                "",
                "```text",
                str(item.get("preview") or ""),
                "```",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect one sealed candidate batch without publishing it.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root-dir", type=Path)
    mode.add_argument("--remote")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args()

    root_base = args.root.strip("/")
    publish_rel = str(PurePosixPath(root_base) / DEFAULT_PUBLISH)
    validation_rel = str(PurePosixPath(root_base) / DEFAULT_VALIDATION)
    update_rel = str(PurePosixPath(root_base) / DEFAULT_UPDATE)
    batch_rel = str(PurePosixPath(root_base) / DEFAULT_BATCH)

    if args.root_dir is not None:
        root = args.root_dir.resolve()
        reader = lambda relative: read_local(root, relative)
    else:
        if not shutil.which("rclone"):
            print("candidate_batch_inspect_error code=rclone_missing canonical_write=0")
            return 2
        remote = str(args.remote)
        reader = lambda relative: read_remote(remote, relative)

    report, errors = inspect_batch(
        master_raw=reader(DEFAULT_MASTER),
        publish_raw=reader(publish_rel),
        validation_raw=reader(validation_rel),
        update_raw=reader(update_rel),
        batch_raw=reader(batch_rel),
        read_content=reader,
    )
    if errors or report is None:
        print(
            "candidate_batch_inspect_error "
            f"codes={','.join(errors or ['inspection_failed'])} canonical_write=0"
        )
        return 2

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown_out is not None:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(render_markdown(report), encoding="utf-8")

    counts = report["counts"]
    print(
        "candidate_batch_inspect_ok "
        f"state={report['state']} batch_id={report.get('batch_id')} items={counts['items']} "
        f"create={counts['create']} update={counts['update']} unchanged={counts['unchanged']} "
        "live_preconditions=1 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
