#!/usr/bin/env python3
"""Deterministic canonical record mutation engine for candidate transactions.

This module is intentionally transport-agnostic and has no CLI, rclone integration,
Git coordination, index rebuild, receipt handling, or settlement behavior.

It validates and executes only the record-mutation portion of one already sealed
transaction:
- CREATE requires target absence
- UPDATE requires the exact planned base SHA
- UNCHANGED requires exact expected bytes
- candidate content must match the planned SHA
- every CREATE/UPDATE is rechecked immediately before write
- every write is verified byte-for-byte immediately after write

If a write fails after any earlier write succeeded, the result is RECOVERY_REQUIRED.
The caller must use the durable recovery journal prepared before mutation. This module
never guesses at rollback state itself.
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any, Callable

from candidate_transaction_plan import TRANSACTION_PLAN_VERSION

SCHEMA_VERSION = 1
MUTATION_ENGINE_VERSION = "0.1.0"
MAX_ITEMS = 50

ReadBytes = Callable[[str], bytes | None]
WriteBytes = Callable[[str, bytes], bool]


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def safe_path(value: str, prefix: str) -> str | None:
    try:
        path = PurePosixPath(value.replace("\\", "/"))
    except Exception:
        return None
    if path.is_absolute() or not path.parts or ".." in path.parts:
        return None
    normalized = path.as_posix().strip("/")
    if not normalized.startswith(prefix):
        return None
    return normalized


def validate_transaction_for_mutation(plan: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(plan, dict):
        return [], ["transaction_invalid"]

    errors: list[str] = []
    if plan.get("schema_version") != SCHEMA_VERSION:
        errors.append("transaction_schema")
    if plan.get("transaction_plan_version") != TRANSACTION_PLAN_VERSION:
        errors.append("transaction_version")
    if plan.get("canonical_write_performed") is not False:
        errors.append("transaction_write_flag")
    if plan.get("requires_live_revalidation") is not True:
        errors.append("transaction_live_revalidation")
    if plan.get("requires_byte_verification_after_write") not in {True, False}:
        errors.append("transaction_byte_verification_flag")
    if plan.get("requires_rollback_on_partial_failure") not in {True, False}:
        errors.append("transaction_rollback_flag")

    items = plan.get("items")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        errors.append("transaction_items")
        return [], sorted(set(errors))

    normalized: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            errors.append(f"item_{index}_invalid")
            continue

        action = raw.get("action")
        if action not in {"CREATE", "UPDATE", "UNCHANGED"}:
            errors.append(f"item_{index}_action")
            continue

        target = safe_path(str(raw.get("target_path") or ""), "00_LIBRARY/")
        content_path = safe_path(str(raw.get("content_path") or ""), "99_INBOX/CANDIDATES/")
        if target is None:
            errors.append(f"item_{index}_target")
            continue
        if content_path is None:
            errors.append(f"item_{index}_content_path")
            continue
        if target in seen_targets:
            errors.append(f"item_{index}_duplicate_target")
            continue
        seen_targets.add(target)

        digest = raw.get("content_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            errors.append(f"item_{index}_content_sha")
            continue

        base_sha = raw.get("base_sha256")
        if action == "UPDATE" and (not isinstance(base_sha, str) or len(base_sha) != 64):
            errors.append(f"item_{index}_base_sha")
            continue

        expected_precondition = {
            "CREATE": "TARGET_ABSENT",
            "UPDATE": "EXACT_BASE_SHA",
            "UNCHANGED": "EXACT_BYTES_PRESENT",
        }[action]
        if raw.get("precondition") != expected_precondition:
            errors.append(f"item_{index}_precondition")
            continue

        normalized.append(
            {
                **raw,
                "target_path": target,
                "content_path": content_path,
            }
        )

    return normalized, sorted(set(errors))


def preflight_record_mutations(
    plan: dict[str, Any] | None,
    read_bytes: ReadBytes,
) -> tuple[list[dict[str, Any]], dict[str, bytes], list[str]]:
    items, errors = validate_transaction_for_mutation(plan)
    if errors:
        return [], {}, errors

    expected: dict[str, bytes] = {}
    preflight_errors: list[str] = []
    for index, item in enumerate(items):
        content = read_bytes(item["content_path"])
        if content is None:
            preflight_errors.append(f"item_{index}_content_missing")
            continue
        if sha256(content) != item["content_sha256"]:
            preflight_errors.append(f"item_{index}_content_sha_changed")
            continue

        current = read_bytes(item["target_path"])
        action = item["action"]
        if action == "CREATE" and current is not None:
            preflight_errors.append(f"item_{index}_create_target_exists")
        elif action == "UPDATE":
            if current is None:
                preflight_errors.append(f"item_{index}_update_target_missing")
            elif sha256(current) != item["base_sha256"]:
                preflight_errors.append(f"item_{index}_update_base_sha_changed")
        elif action == "UNCHANGED" and current != content:
            preflight_errors.append(f"item_{index}_unchanged_bytes_changed")

        expected[item["target_path"]] = content

    return items, expected, sorted(set(preflight_errors))


def execute_record_mutations(
    plan: dict[str, Any] | None,
    *,
    read_bytes: ReadBytes,
    write_bytes: WriteBytes,
) -> tuple[dict[str, Any] | None, list[str]]:
    items, expected, errors = preflight_record_mutations(plan, read_bytes)
    if errors or not isinstance(plan, dict):
        return None, errors or ["preflight_failed"]

    writes_completed = 0
    verified_items = 0
    mutation_started = False

    for index, item in enumerate(items):
        action = item["action"]
        target = item["target_path"]
        content = expected[target]

        if action == "UNCHANGED":
            if read_bytes(target) != content:
                state = "RECOVERY_REQUIRED" if mutation_started else "PREWRITE_ABORT"
                return {
                    "schema_version": SCHEMA_VERSION,
                    "mutation_engine_version": MUTATION_ENGINE_VERSION,
                    "transaction_id": plan.get("transaction_id"),
                    "state": state,
                    "writes_completed": writes_completed,
                    "verified_items": verified_items,
                    "canonical_mutation_started": mutation_started,
                }, [f"item_{index}_unchanged_recheck_failed"]
            verified_items += 1
            continue

        # Recheck the exact live precondition immediately before each mutation.
        current = read_bytes(target)
        if action == "CREATE":
            if current is not None:
                state = "RECOVERY_REQUIRED" if mutation_started else "PREWRITE_ABORT"
                return {
                    "schema_version": SCHEMA_VERSION,
                    "mutation_engine_version": MUTATION_ENGINE_VERSION,
                    "transaction_id": plan.get("transaction_id"),
                    "state": state,
                    "writes_completed": writes_completed,
                    "verified_items": verified_items,
                    "canonical_mutation_started": mutation_started,
                }, [f"item_{index}_create_recheck_failed"]
        elif action == "UPDATE":
            if current is None or sha256(current) != item["base_sha256"]:
                state = "RECOVERY_REQUIRED" if mutation_started else "PREWRITE_ABORT"
                return {
                    "schema_version": SCHEMA_VERSION,
                    "mutation_engine_version": MUTATION_ENGINE_VERSION,
                    "transaction_id": plan.get("transaction_id"),
                    "state": state,
                    "writes_completed": writes_completed,
                    "verified_items": verified_items,
                    "canonical_mutation_started": mutation_started,
                }, [f"item_{index}_update_recheck_failed"]

        mutation_started = True
        if not write_bytes(target, content):
            return {
                "schema_version": SCHEMA_VERSION,
                "mutation_engine_version": MUTATION_ENGINE_VERSION,
                "transaction_id": plan.get("transaction_id"),
                "state": "RECOVERY_REQUIRED",
                "writes_completed": writes_completed,
                "verified_items": verified_items,
                "canonical_mutation_started": True,
            }, [f"item_{index}_write_failed"]

        writes_completed += 1
        if read_bytes(target) != content:
            return {
                "schema_version": SCHEMA_VERSION,
                "mutation_engine_version": MUTATION_ENGINE_VERSION,
                "transaction_id": plan.get("transaction_id"),
                "state": "RECOVERY_REQUIRED",
                "writes_completed": writes_completed,
                "verified_items": verified_items,
                "canonical_mutation_started": True,
            }, [f"item_{index}_write_verify_failed"]
        verified_items += 1

    return {
        "schema_version": SCHEMA_VERSION,
        "mutation_engine_version": MUTATION_ENGINE_VERSION,
        "transaction_id": plan.get("transaction_id"),
        "state": "RECORDS_VERIFIED",
        "writes_completed": writes_completed,
        "verified_items": verified_items,
        "canonical_mutation_started": mutation_started,
    }, []
