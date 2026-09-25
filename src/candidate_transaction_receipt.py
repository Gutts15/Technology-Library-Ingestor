#!/usr/bin/env python3
"""Build a verified receipt from an already completed candidate transaction state.

This module performs no writes. It verifies that the current MASTER_INDEX and every
canonical target match the transaction plan's expected post-transaction bytes and
returns a compact receipt payload. Candidate provenance from the transaction plan
is preserved so a later lifecycle settlement stage can prove which private
candidate produced each committed record.

Building a receipt never authorizes or performs canonical publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

from candidate_transaction_plan import TRANSACTION_PLAN_VERSION, read_json

SCHEMA_VERSION = 1
RECEIPT_VERSION = "0.2.0"
MAX_ITEMS = 50
ID_RE = re.compile(r"^[0-9a-f]{20}$")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _optional_id(item: dict[str, Any], key: str, index: int, errors: list[str]) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        errors.append(f"item_{index}_{key}")
        return None
    return value


def build_receipt(
    plan: dict[str, Any] | None,
    master_before: bytes,
    master_after: bytes,
    read_content: Callable[[str], bytes | None],
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if not isinstance(plan, dict):
        return None, ["transaction_plan_invalid"]
    if plan.get("schema_version") != SCHEMA_VERSION:
        errors.append("transaction_schema")
    if plan.get("transaction_plan_version") != TRANSACTION_PLAN_VERSION:
        errors.append("transaction_version")
    if plan.get("canonical_write_performed") is not False:
        errors.append("transaction_plan_write_flag")

    before_sha = sha256(master_before)
    if before_sha != plan.get("master_index_sha256"):
        errors.append("master_before_sha_mismatch")
    if not master_after:
        errors.append("master_after_missing")

    items = plan.get("items")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        errors.append("transaction_items")
        items = []

    receipt_items: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"item_{index}_invalid")
            continue
        lane = item.get("lane")
        action = item.get("action")
        target = item.get("target_path")
        content_path = item.get("content_path")
        expected_sha = item.get("content_sha256")
        record_id = item.get("record_id")
        if lane not in {"NEW", "UPDATE"}:
            errors.append(f"item_{index}_lane")
        if action not in {"CREATE", "UPDATE", "UNCHANGED"}:
            errors.append(f"item_{index}_action")
        if not isinstance(record_id, str) or not ID_RE.fullmatch(record_id):
            errors.append(f"item_{index}_record_id")
        if not isinstance(target, str) or not target.startswith("00_LIBRARY/"):
            errors.append(f"item_{index}_target")
            continue
        if not isinstance(content_path, str) or not content_path.startswith("99_INBOX/CANDIDATES/"):
            errors.append(f"item_{index}_content_path")
            continue
        if not isinstance(expected_sha, str) or len(expected_sha) != 64:
            errors.append(f"item_{index}_content_sha")
            continue

        package_id = _optional_id(item, "package_id", index, errors)
        revision_key = _optional_id(item, "revision_key", index, errors)
        candidate_id = _optional_id(item, "candidate_id", index, errors)
        if lane == "NEW" and (package_id is None or revision_key is None):
            errors.append(f"item_{index}_new_provenance_missing")
        if lane == "UPDATE" and candidate_id is None:
            errors.append(f"item_{index}_update_provenance_missing")

        expected = read_content(content_path)
        actual = read_content(target)
        if expected is None:
            errors.append(f"item_{index}_expected_content_missing")
            continue
        if sha256(expected) != expected_sha:
            errors.append(f"item_{index}_expected_content_sha_changed")
            continue
        if actual != expected:
            errors.append(f"item_{index}_target_bytes_mismatch")
            continue

        row: dict[str, Any] = {
            "lane": lane,
            "action": action,
            "record_id": record_id,
            "target_path": target,
            "content_sha256": expected_sha,
        }
        if package_id is not None:
            row["package_id"] = package_id
        if revision_key is not None:
            row["revision_key"] = revision_key
        if candidate_id is not None:
            row["candidate_id"] = candidate_id
        receipt_items.append(row)

    if errors:
        return None, sorted(set(errors))

    after_sha = sha256(master_after)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "receipt_version": RECEIPT_VERSION,
        "transaction_id": plan.get("transaction_id"),
        "transaction_plan_version": plan.get("transaction_plan_version"),
        "state": "VERIFIED_COMMITTED_STATE",
        "master_index_before_sha256": before_sha,
        "master_index_after_sha256": after_sha,
        "master_index_changed": before_sha != after_sha,
        "items": sorted(receipt_items, key=lambda row: (str(row["target_path"]), str(row["record_id"]))),
        "counts": {
            "items": len(receipt_items),
            "create": sum(1 for row in receipt_items if row["action"] == "CREATE"),
            "update": sum(1 for row in receipt_items if row["action"] == "UPDATE"),
            "unchanged": sum(1 for row in receipt_items if row["action"] == "UNCHANGED"),
        },
        "verification_only": True,
        "candidate_settlement_eligible": True,
        "canonical_write_performed": False,
    }
    return receipt, []


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a verified candidate transaction receipt without writing canonical state.")
    parser.add_argument("--transaction-plan", type=Path, required=True)
    parser.add_argument("--root-dir", type=Path, required=True)
    parser.add_argument("--master-before", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    plan = read_json(args.transaction_plan)
    try:
        master_before = args.master_before.read_bytes()
        master_after = (args.root_dir / "00_LIBRARY/MASTER_INDEX.md").read_bytes()
    except OSError:
        print("candidate_transaction_receipt_error code=master_read_failed canonical_write=0")
        return 2

    root = args.root_dir.resolve()
    reader = lambda relative: (root / relative).read_bytes() if (root / relative).exists() else None
    receipt, errors = build_receipt(plan, master_before, master_after, reader)
    if errors or receipt is None:
        print(
            "candidate_transaction_receipt_error "
            f"codes={','.join(errors or ['receipt_failed'])} canonical_write=0"
        )
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_transaction_receipt_ok "
        f"transaction_id={receipt['transaction_id']} items={receipt['counts']['items']} "
        f"master_changed={1 if receipt['master_index_changed'] else 0} canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
