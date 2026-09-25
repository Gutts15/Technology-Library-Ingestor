#!/usr/bin/env python3
"""Prepare a private candidate transaction handoff with live read-only revalidation.

This stage consumes only control files sealed by FINALIZE_BATCH/current.json. The
batch barrier binds the exact current MASTER_INDEX plus the exact bytes of the
new-record manifest, its dry-run validation plan, and UPDATE_READY/latest.json.
Transaction preparation then rechecks candidate content and canonical target
preconditions before optionally persisting one private TRANSACTION_READY plan.

`--apply` means apply private planning state only. This module never writes to
00_LIBRARY and cannot invoke the canonical publisher.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_finalize_batch import verify_finalize_batch
from candidate_publish_index import parse_candidate_manifest
from candidate_transaction_plan import build_transaction_plan
from candidate_update_ready import UPDATE_READY_VERSION
from library_publish import derive_target

SCHEMA_VERSION = 1
PREPARE_VERSION = "0.3.0"
DEFAULT_ROOT = "99_INBOX/CANDIDATES"
DEFAULT_NEW_MANIFEST = "PUBLISH_READY/latest.json"
DEFAULT_NEW_VALIDATION = "PUBLISH_READY/dry-run-plan.json"
DEFAULT_UPDATE_MANIFEST = "UPDATE_READY/latest.json"
DEFAULT_FINALIZE_BATCH = "FINALIZE_BATCH/current.json"
DEFAULT_TRANSACTION_READY = "TRANSACTION_READY/plan.json"
DEFAULT_MASTER = "00_LIBRARY/MASTER_INDEX.md"
MAX_UPDATE_ARTIFACTS = 50


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


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


def read_local(root: Path, relative: str) -> bytes | None:
    try:
        return (root / relative).read_bytes()
    except OSError:
        return None


def read_remote(remote: str, relative: str) -> bytes | None:
    result = run_rclone(["cat", join_remote(remote, relative), "--log-level", "ERROR"])
    return result.stdout if result.returncode == 0 else None


def parse_update_manifest(payload: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[str]]:
    if payload is None:
        return [], ["update_ready_manifest_missing"]
    if not isinstance(payload, dict):
        return [], ["update_ready_manifest_invalid"]
    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("update_ready_manifest_schema")
    if payload.get("update_ready_version") != UPDATE_READY_VERSION:
        errors.append("update_ready_manifest_version")
    if payload.get("canonical_write_performed") is not False:
        errors.append("update_ready_manifest_write_flag")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > MAX_UPDATE_ARTIFACTS:
        errors.append("update_ready_manifest_items")
        return [], sorted(set(errors))
    output: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    seen_targets: set[str] = set()
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            errors.append(f"update_ready_{index}_invalid")
            continue
        candidate_id = raw.get("candidate_id")
        target = raw.get("target_path")
        if not isinstance(candidate_id, str) or len(candidate_id) != 20:
            errors.append(f"update_ready_{index}_candidate_id")
        elif candidate_id in seen_candidates:
            errors.append(f"update_ready_{index}_duplicate_candidate")
        else:
            seen_candidates.add(candidate_id)
        if not isinstance(target, str) or not target.startswith("00_LIBRARY/"):
            errors.append(f"update_ready_{index}_target")
        elif target in seen_targets:
            errors.append(f"update_ready_{index}_duplicate_target")
        else:
            seen_targets.add(target)
        output.append(dict(raw))
    if errors:
        return [], sorted(set(errors))
    return output, []


def validate_live_inputs(
    master_content: bytes,
    new_manifest: dict[str, Any] | None,
    new_validation: dict[str, Any] | None,
    update_artifacts: list[dict[str, Any]],
    read_content,
) -> list[str]:
    errors: list[str] = []
    if not master_content:
        errors.append("master_missing")

    if new_manifest is None or new_validation is None:
        errors.append("new_lane_pair_required")
    else:
        try:
            new_items = parse_candidate_manifest(new_manifest)
        except ValueError as exc:
            errors.append(f"new_manifest_{exc}")
            new_items = []
        raw_plan_items = new_validation.get("items") if isinstance(new_validation, dict) else None
        by_target = {
            item.get("target_path"): item
            for item in raw_plan_items
            if isinstance(item, dict) and isinstance(item.get("target_path"), str)
        } if isinstance(raw_plan_items, list) else {}
        for index, item in enumerate(new_items):
            content = read_content(item["content_path"])
            if content is None:
                errors.append(f"new_{index}_content_missing")
                continue
            if sha256(content) != item["content_sha256"]:
                errors.append(f"new_{index}_content_sha_changed")
                continue
            target = derive_target(
                record_type=item["record_type"],
                domain=item["domain"],
                category=item["category"],
                slug=item["slug"],
            )
            action = by_target.get(target, {}).get("action")
            current = read_content(target)
            if action == "CREATE" and current is not None:
                errors.append(f"new_{index}_create_target_exists")
            elif action == "UNCHANGED" and current != content:
                errors.append(f"new_{index}_unchanged_target_changed")
            elif action not in {"CREATE", "UNCHANGED"}:
                errors.append(f"new_{index}_validation_action_missing")

    for index, artifact in enumerate(update_artifacts):
        if not isinstance(artifact, dict):
            errors.append(f"update_{index}_invalid")
            continue
        content_path = artifact.get("content_path")
        merged_sha = artifact.get("merged_sha256")
        target = artifact.get("target_path")
        base_sha = artifact.get("base_sha256")
        if not all(isinstance(value, str) and value for value in (content_path, merged_sha, target, base_sha)):
            errors.append(f"update_{index}_fields")
            continue
        content = read_content(content_path)
        if content is None:
            errors.append(f"update_{index}_content_missing")
        elif sha256(content) != merged_sha:
            errors.append(f"update_{index}_content_sha_changed")
        current = read_content(target)
        if current is None:
            errors.append(f"update_{index}_target_missing")
        elif sha256(current) != base_sha:
            errors.append(f"update_{index}_base_sha_changed")
    return sorted(set(errors))


def write_local_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(prefix="candidate-transaction-", dir=path.parent, delete=False) as handle:
        handle.write(data)
        temp = Path(handle.name)
    temp.replace(path)


def write_remote_atomic(
    remote: str,
    relative: str,
    payload: dict[str, Any],
    *,
    root: str = DEFAULT_ROOT,
) -> bool:
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    transaction_id = str(payload.get("transaction_id") or "invalid")
    stage = str(PurePosixPath(root) / "TRANSACTION_PREPARING" / f"{transaction_id}.json")
    with tempfile.NamedTemporaryFile(prefix="candidate-transaction-", delete=False) as handle:
        handle.write(data)
        local_name = handle.name
    try:
        for parent in (str(PurePosixPath(relative).parent), str(PurePosixPath(stage).parent)):
            if run_rclone(["mkdir", join_remote(remote, parent), "--log-level", "ERROR"]).returncode != 0:
                return False
        if run_rclone(["copyto", local_name, join_remote(remote, stage), "--log-level", "ERROR"]).returncode != 0:
            return False
        move = run_rclone([
            "moveto",
            join_remote(remote, stage),
            join_remote(remote, relative),
            "--log-level",
            "ERROR",
        ])
        return move.returncode == 0
    finally:
        try:
            Path(local_name).unlink()
        except OSError:
            pass


def prepare_transaction(
    *,
    master_content: bytes,
    new_manifest: dict[str, Any] | None,
    new_validation: dict[str, Any] | None,
    update_artifacts: list[dict[str, Any]],
    read_content,
    finalize_batch_id: str | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    live_errors = validate_live_inputs(
        master_content,
        new_manifest,
        new_validation,
        update_artifacts,
        read_content,
    )
    if live_errors:
        return None, live_errors
    plan, errors = build_transaction_plan(
        master_content,
        new_manifest,
        new_validation,
        update_artifacts,
    )
    if errors or plan is None:
        return None, errors or ["transaction_plan_failed"]
    plan = dict(plan)
    plan["prepare_version"] = PREPARE_VERSION
    plan["finalize_batch_id"] = finalize_batch_id
    plan["live_preconditions_verified"] = True
    plan["canonical_write_performed"] = False
    return plan, []


def _existing_conflict(existing: bytes | None, plan: dict[str, Any]) -> bool:
    if existing is None:
        return False
    current = read_json_bytes(existing)
    return current != plan


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a live-revalidated private candidate transaction plan."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root-dir", type=Path)
    mode.add_argument("--remote")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    root_base = args.root.strip("/")
    manifest_rel = f"{root_base}/{DEFAULT_NEW_MANIFEST}"
    validation_rel = f"{root_base}/{DEFAULT_NEW_VALIDATION}"
    update_manifest_rel = f"{root_base}/{DEFAULT_UPDATE_MANIFEST}"
    finalize_batch_rel = f"{root_base}/{DEFAULT_FINALIZE_BATCH}"
    transaction_rel = f"{root_base}/{DEFAULT_TRANSACTION_READY}"

    if args.root_dir is not None:
        root = args.root_dir.resolve()
        master_raw = read_local(root, DEFAULT_MASTER)
        manifest_raw = read_local(root, manifest_rel)
        validation_raw = read_local(root, validation_rel)
        update_manifest_raw = read_local(root, update_manifest_rel)
        finalize_batch_raw = read_local(root, finalize_batch_rel)
        transaction_existing = read_local(root, transaction_rel)
        reader = lambda relative: read_local(root, relative)
    else:
        if not shutil.which("rclone"):
            print("candidate_transaction_prepare_error code=rclone_missing canonical_write=0")
            return 2
        remote = str(args.remote)
        master_raw = read_remote(remote, DEFAULT_MASTER)
        manifest_raw = read_remote(remote, manifest_rel)
        validation_raw = read_remote(remote, validation_rel)
        update_manifest_raw = read_remote(remote, update_manifest_rel)
        finalize_batch_raw = read_remote(remote, finalize_batch_rel)
        transaction_existing = read_remote(remote, transaction_rel)
        reader = lambda relative: read_remote(remote, relative)

    if master_raw is None:
        print("candidate_transaction_prepare_error code=master_missing canonical_write=0")
        return 2
    manifest = read_json_bytes(manifest_raw)
    validation = read_json_bytes(validation_raw)
    update_manifest = read_json_bytes(update_manifest_raw)
    finalize_batch = read_json_bytes(finalize_batch_raw)

    batch_errors = verify_finalize_batch(
        finalize_batch,
        master_raw,
        manifest_raw or b"",
        validation_raw or b"",
        update_manifest_raw or b"",
    )
    if batch_errors:
        print(
            "candidate_transaction_prepare_error "
            f"codes={','.join(batch_errors)} canonical_write=0"
        )
        return 2

    updates, update_manifest_errors = parse_update_manifest(update_manifest)
    if update_manifest_errors:
        print(
            "candidate_transaction_prepare_error "
            f"codes={','.join(update_manifest_errors)} canonical_write=0"
        )
        return 2

    batch_id = finalize_batch.get("batch_id") if isinstance(finalize_batch, dict) else None
    plan, errors = prepare_transaction(
        master_content=master_raw,
        new_manifest=manifest,
        new_validation=validation,
        update_artifacts=updates,
        read_content=reader,
        finalize_batch_id=batch_id if isinstance(batch_id, str) else None,
    )
    if errors or plan is None:
        print(
            "candidate_transaction_prepare_error "
            f"codes={','.join(errors or ['prepare_failed'])} canonical_write=0"
        )
        return 2

    if args.apply:
        if _existing_conflict(transaction_existing, plan):
            print("candidate_transaction_prepare_error code=transaction_ready_conflict canonical_write=0")
            return 2
        if transaction_existing is None:
            if args.root_dir is not None:
                write_local_atomic(args.root_dir.resolve() / transaction_rel, plan)
            elif not write_remote_atomic(str(args.remote), transaction_rel, plan, root=args.root):
                print("candidate_transaction_prepare_error code=private_plan_write_failed canonical_write=0")
                return 2
            stored = reader(transaction_rel)
            if read_json_bytes(stored) != plan:
                print("candidate_transaction_prepare_error code=private_plan_verify_failed canonical_write=0")
                return 2

    mode_name = "apply_private" if args.apply else "dry_run"
    print(
        "candidate_transaction_prepare_ok "
        f"mode={mode_name} transaction_id={plan['transaction_id']} "
        f"finalize_batch={plan.get('finalize_batch_id')} writes={plan['counts']['writes']} "
        f"create={plan['counts']['create']} update={plan['counts']['update']} "
        f"unchanged={plan['counts']['unchanged']} canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
