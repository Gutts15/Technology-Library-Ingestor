#!/usr/bin/env python3
"""Compose record mutation + canonical index rebuild into one deterministic engine.

This module is transport-agnostic. It receives byte readers/writers and an explicit
canonical raw-record path set, then performs:

1. transaction record mutation with immediate byte verification;
2. deterministic MASTER_INDEX/domain INDEX planning from canonical record bytes;
3. index writes with immediate byte verification;
4. final transaction-target + index verification.

It does not acquire the production coordinator, create recovery snapshots, persist a
receipt, or expose a CLI. Any failure after canonical mutation starts is reported as
RECOVERY_REQUIRED so the caller must restore from the durable pre-write journal.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from candidate_canonical_index_rebuild import (
    apply_index_plan,
    build_index_plan,
    verify_index_state,
)
from candidate_canonical_record_mutation import execute_record_mutations

TRANSACTION_ENGINE_VERSION = "0.1.0"

ReadBytes = Callable[[str], bytes | None]
WriteBytes = Callable[[str, bytes], bool]


def post_transaction_record_paths(
    plan: dict[str, Any],
    canonical_record_paths_before: Iterable[str],
) -> list[str]:
    paths = set(canonical_record_paths_before)
    for item in plan.get("items", []):
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        target = item.get("target_path")
        if action == "CREATE" and isinstance(target, str):
            paths.add(target)
    return sorted(paths)


def verify_transaction_targets(
    plan: dict[str, Any],
    *,
    read_bytes: ReadBytes,
) -> list[str]:
    errors: list[str] = []
    for index, item in enumerate(plan.get("items", [])):
        if not isinstance(item, dict):
            errors.append(f"item_{index}_invalid")
            continue
        target = item.get("target_path")
        content_path = item.get("content_path")
        if not isinstance(target, str) or not isinstance(content_path, str):
            errors.append(f"item_{index}_path_invalid")
            continue
        expected = read_bytes(content_path)
        actual = read_bytes(target)
        if expected is None:
            errors.append(f"item_{index}_expected_missing")
        elif actual != expected:
            errors.append(f"item_{index}_final_target_mismatch")
    return sorted(set(errors))


def execute_canonical_transaction(
    plan: dict[str, Any],
    *,
    canonical_record_paths_before: Iterable[str],
    read_bytes: ReadBytes,
    write_record_bytes: WriteBytes,
    write_index_bytes: WriteBytes,
) -> tuple[dict[str, Any], list[str]]:
    record_report, record_errors = execute_record_mutations(
        plan,
        read_bytes=read_bytes,
        write_bytes=write_record_bytes,
    )
    if record_errors or record_report is None:
        return {
            "transaction_engine_version": TRANSACTION_ENGINE_VERSION,
            "state": (
                record_report.get("state")
                if isinstance(record_report, dict)
                else "PREWRITE_ABORT"
            ),
            "record_report": record_report,
            "index_report": None,
            "final_verification": False,
        }, record_errors or ["record_mutation_failed"]

    paths_after = post_transaction_record_paths(plan, canonical_record_paths_before)
    indexes, records, index_plan_errors = build_index_plan(paths_after, read_bytes)
    if index_plan_errors or indexes is None:
        return {
            "transaction_engine_version": TRANSACTION_ENGINE_VERSION,
            "state": "RECOVERY_REQUIRED",
            "record_report": record_report,
            "index_report": None,
            "final_verification": False,
        }, index_plan_errors or ["index_plan_failed"]

    index_report, index_errors = apply_index_plan(
        indexes,
        read_bytes=read_bytes,
        write_bytes=write_index_bytes,
    )
    if index_errors:
        return {
            "transaction_engine_version": TRANSACTION_ENGINE_VERSION,
            "state": "RECOVERY_REQUIRED",
            "record_report": record_report,
            "index_report": index_report,
            "records_after": len(records),
            "final_verification": False,
        }, index_errors

    verification_errors = [
        *verify_transaction_targets(plan, read_bytes=read_bytes),
        *verify_index_state(indexes, read_bytes=read_bytes),
    ]
    if verification_errors:
        return {
            "transaction_engine_version": TRANSACTION_ENGINE_VERSION,
            "state": "RECOVERY_REQUIRED",
            "record_report": record_report,
            "index_report": index_report,
            "records_after": len(records),
            "final_verification": False,
        }, sorted(set(verification_errors))

    return {
        "transaction_engine_version": TRANSACTION_ENGINE_VERSION,
        "state": "CANONICAL_STATE_VERIFIED",
        "record_report": record_report,
        "index_report": index_report,
        "records_after": len(records),
        "generated_indexes": sorted(indexes),
        "final_verification": True,
    }, []
