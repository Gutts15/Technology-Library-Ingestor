#!/usr/bin/env python3
"""Dependency-free smoke tests for post-publication candidate settlement planning."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_settlement_plan import build_settlement_plan


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def main() -> None:
    txid = "a" * 20
    new_candidate_bytes = b"new candidate\n"
    update_candidate_bytes = b"update candidate\n"
    new_candidate_id = sha(new_candidate_bytes)[:20]
    update_candidate_id = sha(update_candidate_bytes)[:20]
    package_id = "b" * 20
    revision_key = "c" * 20
    new_record_id = "d" * 20
    update_record_id = "e" * 20
    new_target = "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-new-tool.md"
    update_target = "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-existing-tool.md"
    new_content_sha = "1" * 64
    update_content_sha = "2" * 64

    transaction_plan = {
        "schema_version": 1,
        "transaction_plan_version": "0.2.0",
        "transaction_id": txid,
        "canonical_write_performed": False,
        "items": [
            {
                "lane": "NEW",
                "action": "CREATE",
                "package_id": package_id,
                "revision_key": revision_key,
                "record_id": new_record_id,
                "target_path": new_target,
                "content_sha256": new_content_sha,
            },
            {
                "lane": "UPDATE",
                "action": "UPDATE",
                "candidate_id": update_candidate_id,
                "record_id": update_record_id,
                "target_path": update_target,
                "content_sha256": update_content_sha,
            },
        ],
    }
    receipt = {
        "schema_version": 1,
        "receipt_version": "0.2.0",
        "transaction_id": txid,
        "state": "VERIFIED_COMMITTED_STATE",
        "candidate_settlement_eligible": True,
        "items": [
            {
                "lane": "NEW",
                "action": "CREATE",
                "package_id": package_id,
                "revision_key": revision_key,
                "record_id": new_record_id,
                "target_path": new_target,
                "content_sha256": new_content_sha,
            },
            {
                "lane": "UPDATE",
                "action": "UPDATE",
                "candidate_id": update_candidate_id,
                "record_id": update_record_id,
                "target_path": update_target,
                "content_sha256": update_content_sha,
            },
        ],
    }
    packages = [
        {
            "schema_version": 1,
            "package_id": package_id,
            "revision_key": revision_key,
            "candidate_id": new_candidate_id,
            "candidate_sha256": sha(new_candidate_bytes),
            "decision": "VALIDATED_NEW",
            "proposed_record": {
                "record_id": new_record_id,
                "target_path": new_target,
            },
        },
        {
            "schema_version": 1,
            "package_id": "f" * 20,
            "revision_key": "9" * 20,
            "candidate_id": update_candidate_id,
            "candidate_sha256": sha(update_candidate_bytes),
            "decision": "VALIDATED_UPDATE",
            "proposed_record": {
                "record_id": None,
                "target_path": update_target,
            },
        },
    ]
    locations = {
        new_candidate_id: {
            "path": "READY_FOR_CURATION/candidate-new.md",
            "sha256": sha(new_candidate_bytes),
        },
        update_candidate_id: {
            "path": "READY_FOR_CURATION/candidate-update.md",
            "sha256": sha(update_candidate_bytes),
        },
    }

    plan, errors = build_settlement_plan(transaction_plan, receipt, packages, locations)
    assert not errors, errors
    assert plan is not None
    assert plan["canonical_publication_verified"] is True
    assert plan["counts"] == {"candidates": 2, "create": 1, "update": 1, "unchanged": 0}
    assert plan["canonical_write_performed"] is False
    assert plan["private_lifecycle_write_performed"] is False
    destinations = {item["destination_path"] for item in plan["items"]}
    assert all(path.startswith(f"RESOLVED/PUBLISHED/{txid}/") for path in destinations)

    stale_receipt = dict(receipt)
    stale_receipt["transaction_id"] = "0" * 20
    blocked, blocked_errors = build_settlement_plan(
        transaction_plan, stale_receipt, packages, locations
    )
    assert blocked is None
    assert "transaction_id_mismatch" in blocked_errors

    bad_locations = {key: dict(value) for key, value in locations.items()}
    bad_locations[new_candidate_id]["sha256"] = "3" * 64
    blocked, blocked_errors = build_settlement_plan(
        transaction_plan, receipt, packages, bad_locations
    )
    assert blocked is None
    assert "item_0_candidate_sha_mismatch" in blocked_errors

    missing_package = packages[1:]
    blocked, blocked_errors = build_settlement_plan(
        transaction_plan, receipt, missing_package, locations
    )
    assert blocked is None
    assert "item_0_curation_package_missing" in blocked_errors

    print(
        "candidate_settlement_plan_smoke_ok receipt_bound=1 candidate_sha_bound=1 "
        "published_bucket=1 canonical_write=0 private_lifecycle_write=0"
    )


if __name__ == "__main__":
    main()
