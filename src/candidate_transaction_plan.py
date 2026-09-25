#!/usr/bin/env python3
"""Build a deterministic, non-publishing canonical transaction plan.

The plan is the handoff contract between candidate curation and any future
canonical apply implementation. It combines already validated new-record actions
with current-batch SHA-bound UPDATE_READY artifacts, binds the transaction to the
current MASTER_INDEX bytes, rejects cross-lane target collisions, and preserves
enough candidate provenance for verified post-publication settlement.

This module has no --apply mode and never writes to 00_LIBRARY.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from candidate_publish_index import parse_candidate_manifest
from library_publish import derive_target

SCHEMA_VERSION = 1
TRANSACTION_PLAN_VERSION = "0.2.0"
CANDIDATE_VALIDATE_VERSION = "0.1.0"
UPDATE_RENDER_VERSION = "0.1.0"
UPDATE_READY_VERSION = "0.2.0"
MAX_ITEMS = 50
MAX_PLAN_BYTES = 256 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[0-9a-f]{20}$")


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def validate_new_lane(
    manifest: dict[str, Any] | None,
    validation_plan: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if manifest is None and validation_plan is None:
        return [], []
    if not isinstance(manifest, dict):
        return [], ["new_manifest_missing"]
    if not isinstance(validation_plan, dict):
        return [], ["new_validation_plan_missing"]
    try:
        manifest_items = parse_candidate_manifest(manifest)
    except ValueError as exc:
        return [], [f"new_manifest_{exc}"]

    if validation_plan.get("schema_version") != 1:
        return [], ["new_validation_schema"]
    if validation_plan.get("validate_version") != CANDIDATE_VALIDATE_VERSION:
        return [], ["new_validation_version"]
    if validation_plan.get("canonical_write_performed") is not False:
        return [], ["new_validation_write_flag"]
    raw_items = validation_plan.get("items")
    if not isinstance(raw_items, list):
        return [], ["new_validation_items"]

    by_target: dict[str, dict[str, str]] = {}
    for item in manifest_items:
        target = derive_target(
            record_type=item["record_type"],
            domain=item["domain"],
            category=item["category"],
            slug=item["slug"],
        )
        by_target[target] = item

    errors: list[str] = []
    output: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            errors.append(f"new_item_{index}_invalid")
            continue
        target = raw.get("target_path")
        record_id = raw.get("record_id")
        sha = raw.get("content_sha256")
        action = raw.get("action")
        if action not in {"CREATE", "UNCHANGED"}:
            errors.append(f"new_item_{index}_action")
            continue
        if not isinstance(target, str) or target not in by_target:
            errors.append(f"new_item_{index}_target")
            continue
        manifest_item = by_target[target]
        if record_id != manifest_item["record_id"]:
            errors.append(f"new_item_{index}_record_id")
        if sha != manifest_item["content_sha256"]:
            errors.append(f"new_item_{index}_sha")
        if target in seen_targets:
            errors.append(f"new_item_{index}_duplicate_target")
        seen_targets.add(target)
        output.append(
            {
                "lane": "NEW",
                "action": action,
                "package_id": manifest_item["package_id"],
                "revision_key": manifest_item["revision_key"],
                "record_id": manifest_item["record_id"],
                "record_type": manifest_item["record_type"],
                "title": manifest_item["title"],
                "target_path": target,
                "content_path": manifest_item["content_path"],
                "content_sha256": manifest_item["content_sha256"],
                "precondition": "TARGET_ABSENT" if action == "CREATE" else "EXACT_BYTES_PRESENT",
            }
        )
    if len(raw_items) != len(manifest_items):
        errors.append("new_lane_item_count_mismatch")
    if set(by_target) != seen_targets:
        errors.append("new_lane_target_set_mismatch")
    return output, sorted(set(errors))


def validate_update_artifact(payload: dict[str, Any] | None, index: int) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(payload, dict):
        return None, [f"update_{index}_invalid"]
    errors: list[str] = []
    if payload.get("schema_version") != 1:
        errors.append(f"update_{index}_schema")
    if payload.get("update_ready_version") != UPDATE_READY_VERSION:
        errors.append(f"update_{index}_ready_version")
    if payload.get("merge_render_version") != UPDATE_RENDER_VERSION:
        errors.append(f"update_{index}_version")
    if payload.get("canonical_write_performed") is not False:
        errors.append(f"update_{index}_write_flag")
    if payload.get("requires_base_sha_match_before_write") is not True:
        errors.append(f"update_{index}_base_guard")

    candidate_id = payload.get("candidate_id")
    record_id = payload.get("record_id")
    target = payload.get("target_path")
    base_sha = payload.get("base_sha256")
    merged_sha = payload.get("merged_sha256")
    content_path = payload.get("content_path")
    if not isinstance(candidate_id, str) or not ID_RE.fullmatch(candidate_id):
        errors.append(f"update_{index}_candidate_id")
    if not isinstance(record_id, str) or not ID_RE.fullmatch(record_id):
        errors.append(f"update_{index}_record_id")
    if not isinstance(target, str) or not target.startswith("00_LIBRARY/") or not target.endswith(".md"):
        errors.append(f"update_{index}_target")
    if not isinstance(base_sha, str) or not SHA_RE.fullmatch(base_sha):
        errors.append(f"update_{index}_base_sha")
    if not isinstance(merged_sha, str) or not SHA_RE.fullmatch(merged_sha):
        errors.append(f"update_{index}_merged_sha")
    if not isinstance(content_path, str) or not content_path.startswith("99_INBOX/CANDIDATES/UPDATE_READY/records/"):
        errors.append(f"update_{index}_content_path")
    if errors:
        return None, sorted(set(errors))
    return {
        "lane": "UPDATE",
        "action": "UPDATE",
        "candidate_id": candidate_id,
        "record_id": record_id,
        "target_path": target,
        "content_path": content_path,
        "content_sha256": merged_sha,
        "base_sha256": base_sha,
        "precondition": "EXACT_BASE_SHA",
    }, []


def build_transaction_plan(
    master_content: bytes,
    new_manifest: dict[str, Any] | None,
    new_validation_plan: dict[str, Any] | None,
    update_artifacts: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    if not master_content:
        return None, ["master_missing"]
    try:
        master_content.decode("utf-8")
    except UnicodeDecodeError:
        return None, ["master_utf8"]

    new_items, new_errors = validate_new_lane(new_manifest, new_validation_plan)
    errors = list(new_errors)
    update_items: list[dict[str, Any]] = []
    for index, payload in enumerate(update_artifacts):
        item, item_errors = validate_update_artifact(payload, index)
        errors.extend(item_errors)
        if item is not None:
            update_items.append(item)

    items = new_items + update_items
    if len(items) > MAX_ITEMS:
        errors.append("too_many_items")
    seen_targets: set[str] = set()
    seen_ids: set[str] = set()
    for index, item in enumerate(items):
        target = item["target_path"]
        record_id = item["record_id"]
        if target in seen_targets:
            errors.append(f"transaction_item_{index}_target_collision")
        if record_id in seen_ids:
            errors.append(f"transaction_item_{index}_record_id_collision")
        seen_targets.add(target)
        seen_ids.add(record_id)
    if errors:
        return None, sorted(set(errors))

    ordered = sorted(items, key=lambda row: (row["target_path"], row["record_id"], row["lane"]))
    master_sha = hashlib.sha256(master_content).hexdigest()
    write_items = [row for row in ordered if row["action"] in {"CREATE", "UPDATE"}]
    digest_input = {
        "master_sha256": master_sha,
        "items": ordered,
    }
    transaction_id = hashlib.sha256(
        json.dumps(digest_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]

    plan = {
        "schema_version": SCHEMA_VERSION,
        "transaction_plan_version": TRANSACTION_PLAN_VERSION,
        "transaction_id": transaction_id,
        "master_index_sha256": master_sha,
        "canonical_write_performed": False,
        "requires_live_revalidation": True,
        "requires_atomic_record_writes": True,
        "requires_index_rebuild_after_write": bool(write_items),
        "requires_byte_verification_after_write": bool(write_items),
        "requires_rollback_on_partial_failure": bool(write_items),
        "items": ordered,
        "counts": {
            "create": sum(1 for row in ordered if row["action"] == "CREATE"),
            "update": sum(1 for row in ordered if row["action"] == "UPDATE"),
            "unchanged": sum(1 for row in ordered if row["action"] == "UNCHANGED"),
            "writes": len(write_items),
        },
    }
    encoded = json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PLAN_BYTES:
        return None, ["transaction_plan_too_large"]
    return plan, []


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a non-publishing candidate canonical transaction plan.")
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--new-manifest", type=Path)
    parser.add_argument("--new-validation-plan", type=Path)
    parser.add_argument("--update-artifact", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    try:
        master_content = args.master.read_bytes()
    except OSError:
        print("candidate_transaction_plan_error code=master_read_failed canonical_write=0")
        return 2
    new_manifest = read_json(args.new_manifest) if args.new_manifest else None
    new_validation = read_json(args.new_validation_plan) if args.new_validation_plan else None
    updates = [payload for path in args.update_artifact if (payload := read_json(path)) is not None]
    if len(updates) != len(args.update_artifact):
        print("candidate_transaction_plan_error code=update_artifact_read_failed canonical_write=0")
        return 2

    plan, errors = build_transaction_plan(master_content, new_manifest, new_validation, updates)
    if errors or plan is None:
        print(f"candidate_transaction_plan_error codes={','.join(errors or ['plan_failed'])} canonical_write=0")
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_transaction_plan_ok "
        f"transaction_id={plan['transaction_id']} writes={plan['counts']['writes']} "
        f"create={plan['counts']['create']} update={plan['counts']['update']} "
        f"unchanged={plan['counts']['unchanged']} canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
