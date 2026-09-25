#!/usr/bin/env python3
"""Plan post-publication candidate lifecycle settlement without moving anything.

A candidate may leave READY_FOR_CURATION only after a transaction receipt proves
that its canonical record reached the verified committed state. This module binds
that receipt back to the exact curation package and candidate SHA, then produces a
deterministic private lifecycle plan under RESOLVED/PUBLISHED/<transaction_id>.

It never writes to 00_LIBRARY and has no remote/apply mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_transaction_plan import TRANSACTION_PLAN_VERSION, read_json
from candidate_transaction_receipt import RECEIPT_VERSION

SCHEMA_VERSION = 1
SETTLEMENT_PLAN_VERSION = "0.1.0"
MAX_ITEMS = 50
ID_RE = re.compile(r"^[0-9a-f]{20}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _id(value: Any) -> bool:
    return isinstance(value, str) and bool(ID_RE.fullmatch(value))


def _sha(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA_RE.fullmatch(value))


def _safe_ready_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 2:
        return None
    if path.parts[0] != "READY_FOR_CURATION":
        return None
    if not path.name.startswith("candidate-") or path.suffix != ".md":
        return None
    return path.as_posix()


def validate_receipt_binding(
    plan: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(plan, dict):
        return [], ["transaction_plan_invalid"]
    if not isinstance(receipt, dict):
        return [], ["receipt_invalid"]
    errors: list[str] = []
    if plan.get("schema_version") != SCHEMA_VERSION:
        errors.append("transaction_schema")
    if plan.get("transaction_plan_version") != TRANSACTION_PLAN_VERSION:
        errors.append("transaction_version")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        errors.append("receipt_schema")
    if receipt.get("receipt_version") != RECEIPT_VERSION:
        errors.append("receipt_version")
    if receipt.get("state") != "VERIFIED_COMMITTED_STATE":
        errors.append("receipt_state")
    if receipt.get("candidate_settlement_eligible") is not True:
        errors.append("receipt_not_settlement_eligible")
    transaction_id = plan.get("transaction_id")
    if not _id(transaction_id) or receipt.get("transaction_id") != transaction_id:
        errors.append("transaction_id_mismatch")

    plan_items = plan.get("items")
    receipt_items = receipt.get("items")
    if not isinstance(plan_items, list) or len(plan_items) > MAX_ITEMS:
        errors.append("transaction_items")
        plan_items = []
    if not isinstance(receipt_items, list) or len(receipt_items) > MAX_ITEMS:
        errors.append("receipt_items")
        receipt_items = []

    def key(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
        return (
            str(item.get("lane")),
            str(item.get("action")),
            str(item.get("record_id")),
            str(item.get("target_path")),
            str(item.get("content_sha256")),
        )

    receipt_by_key: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for index, raw in enumerate(receipt_items):
        if not isinstance(raw, dict):
            errors.append(f"receipt_item_{index}_invalid")
            continue
        item_key = key(raw)
        if item_key in receipt_by_key:
            errors.append(f"receipt_item_{index}_duplicate")
        receipt_by_key[item_key] = raw

    bound: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str, str, str]] = set()
    for index, raw in enumerate(plan_items):
        if not isinstance(raw, dict):
            errors.append(f"transaction_item_{index}_invalid")
            continue
        item_key = key(raw)
        match = receipt_by_key.get(item_key)
        if match is None:
            errors.append(f"transaction_item_{index}_receipt_missing")
            continue
        seen_keys.add(item_key)
        lane = raw.get("lane")
        if lane == "NEW":
            for field in ("package_id", "revision_key"):
                if not _id(raw.get(field)) or match.get(field) != raw.get(field):
                    errors.append(f"transaction_item_{index}_{field}_mismatch")
        elif lane == "UPDATE":
            if not _id(raw.get("candidate_id")) or match.get("candidate_id") != raw.get("candidate_id"):
                errors.append(f"transaction_item_{index}_candidate_id_mismatch")
        else:
            errors.append(f"transaction_item_{index}_lane")
        bound.append({"plan": raw, "receipt": match})

    if set(receipt_by_key) != seen_keys:
        errors.append("receipt_item_set_mismatch")
    if errors:
        return [], sorted(set(errors))
    return bound, []


def build_settlement_plan(
    transaction_plan: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
    curation_packages: list[dict[str, Any]],
    candidate_locations: dict[str, dict[str, str]],
) -> tuple[dict[str, Any] | None, list[str]]:
    bound, errors = validate_receipt_binding(transaction_plan, receipt)
    if errors or not isinstance(transaction_plan, dict):
        return None, errors

    by_candidate: dict[str, dict[str, Any]] = {}
    by_package: dict[tuple[str, str], dict[str, Any]] = {}
    package_errors: list[str] = []
    for index, package in enumerate(curation_packages):
        if not isinstance(package, dict) or package.get("schema_version") != 1:
            package_errors.append(f"package_{index}_invalid")
            continue
        candidate_id = package.get("candidate_id")
        candidate_sha = package.get("candidate_sha256")
        package_id = package.get("package_id")
        revision_key = package.get("revision_key")
        decision = package.get("decision")
        if not _id(candidate_id):
            package_errors.append(f"package_{index}_candidate_id")
            continue
        if not _sha(candidate_sha):
            package_errors.append(f"package_{index}_candidate_sha")
        if not _id(package_id) or not _id(revision_key):
            package_errors.append(f"package_{index}_revision_binding")
        if decision not in {"VALIDATED_NEW", "VALIDATED_UPDATE", "SOURCE_ONLY"}:
            package_errors.append(f"package_{index}_decision")
        if candidate_id in by_candidate:
            package_errors.append(f"package_{index}_duplicate_candidate")
        if _id(package_id) and _id(revision_key) and (package_id, revision_key) in by_package:
            package_errors.append(f"package_{index}_duplicate_revision")
        by_candidate[str(candidate_id)] = package
        if _id(package_id) and _id(revision_key):
            by_package[(str(package_id), str(revision_key))] = package
    if package_errors:
        return None, sorted(set(package_errors))

    transaction_id = str(transaction_plan["transaction_id"])
    settlement_items: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    for index, pair in enumerate(bound):
        item = pair["plan"]
        lane = item["lane"]
        if lane == "NEW":
            package = by_package.get((item["package_id"], item["revision_key"]))
            if package is None:
                errors.append(f"item_{index}_curation_package_missing")
                continue
            if package.get("decision") not in {"VALIDATED_NEW", "SOURCE_ONLY"}:
                errors.append(f"item_{index}_decision_mismatch")
        else:
            package = by_candidate.get(item["candidate_id"])
            if package is None:
                errors.append(f"item_{index}_curation_package_missing")
                continue
            if package.get("decision") != "VALIDATED_UPDATE":
                errors.append(f"item_{index}_decision_mismatch")

        candidate_id = str(package.get("candidate_id"))
        candidate_sha = package.get("candidate_sha256")
        if candidate_id in seen_candidates:
            errors.append(f"item_{index}_duplicate_candidate")
            continue
        seen_candidates.add(candidate_id)

        location = candidate_locations.get(candidate_id)
        if not isinstance(location, dict):
            errors.append(f"item_{index}_candidate_location_missing")
            continue
        source_path = _safe_ready_path(location.get("path"))
        source_sha = location.get("sha256")
        if source_path is None:
            errors.append(f"item_{index}_candidate_path")
            continue
        if source_sha != candidate_sha or not _sha(source_sha):
            errors.append(f"item_{index}_candidate_sha_mismatch")
            continue

        proposed = package.get("proposed_record")
        if not isinstance(proposed, dict) or proposed.get("target_path") != item.get("target_path"):
            errors.append(f"item_{index}_target_binding_mismatch")
            continue
        if lane == "NEW" and proposed.get("record_id") != item.get("record_id"):
            errors.append(f"item_{index}_record_id_binding_mismatch")
            continue

        destination = str(
            PurePosixPath("RESOLVED") / "PUBLISHED" / transaction_id / PurePosixPath(source_path).name
        )
        settlement_items.append(
            {
                "candidate_id": candidate_id,
                "candidate_sha256": candidate_sha,
                "decision": package.get("decision"),
                "lane": lane,
                "action": item.get("action"),
                "record_id": item.get("record_id"),
                "target_path": item.get("target_path"),
                "source_path": source_path,
                "destination_path": destination,
            }
        )

    if errors:
        return None, sorted(set(errors))

    ordered = sorted(settlement_items, key=lambda row: (str(row["candidate_id"]), str(row["target_path"])))
    return {
        "schema_version": SCHEMA_VERSION,
        "settlement_plan_version": SETTLEMENT_PLAN_VERSION,
        "transaction_id": transaction_id,
        "receipt_version": receipt.get("receipt_version") if isinstance(receipt, dict) else None,
        "canonical_publication_verified": True,
        "private_lifecycle_write_performed": False,
        "canonical_write_performed": False,
        "items": ordered,
        "counts": {
            "candidates": len(ordered),
            "create": sum(1 for row in ordered if row["action"] == "CREATE"),
            "update": sum(1 for row in ordered if row["action"] == "UPDATE"),
            "unchanged": sum(1 for row in ordered if row["action"] == "UNCHANGED"),
        },
    }, []


def _read_candidate_location(path: Path) -> tuple[str, dict[str, str]]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    candidate_id = digest[:20]
    return candidate_id, {
        "path": f"READY_FOR_CURATION/{path.name}",
        "sha256": digest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan verified candidate lifecycle settlement without moving files.")
    parser.add_argument("--transaction-plan", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--curation-package", type=Path, action="append", default=[])
    parser.add_argument("--candidate-file", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    transaction_plan = read_json(args.transaction_plan)
    receipt = read_json(args.receipt)
    packages = [payload for path in args.curation_package if (payload := read_json(path)) is not None]
    if len(packages) != len(args.curation_package):
        print("candidate_settlement_plan_error code=curation_package_read_failed canonical_write=0")
        return 2
    locations: dict[str, dict[str, str]] = {}
    try:
        for path in args.candidate_file:
            candidate_id, location = _read_candidate_location(path)
            if candidate_id in locations:
                print("candidate_settlement_plan_error code=duplicate_candidate_location canonical_write=0")
                return 2
            locations[candidate_id] = location
    except OSError:
        print("candidate_settlement_plan_error code=candidate_read_failed canonical_write=0")
        return 2

    plan, errors = build_settlement_plan(transaction_plan, receipt, packages, locations)
    if errors or plan is None:
        print(
            "candidate_settlement_plan_error "
            f"codes={','.join(errors or ['settlement_failed'])} canonical_write=0"
        )
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_settlement_plan_ok "
        f"transaction_id={plan['transaction_id']} candidates={plan['counts']['candidates']} "
        f"canonical_write=0 private_lifecycle_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
