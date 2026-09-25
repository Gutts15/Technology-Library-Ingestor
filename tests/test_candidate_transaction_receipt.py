#!/usr/bin/env python3
"""Dependency-free smoke tests for the verified transaction receipt contract."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_transaction_receipt import build_receipt


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def main() -> None:
    master_before = b"# MASTER_INDEX\n\nGENERATED: TRUE\nRECORDS: 1\n"
    master_after = b"# MASTER_INDEX\n\nGENERATED: TRUE\nRECORDS: 2\n"
    new_content = b"new canonical bytes\n"
    update_content = b"updated canonical bytes\n"
    new_private = "99_INBOX/CANDIDATES/PUBLISH_READY/records/new.md"
    update_private = "99_INBOX/CANDIDATES/UPDATE_READY/records/update.md"
    new_target = "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-new.md"
    update_target = "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-existing.md"

    plan = {
        "schema_version": 1,
        "transaction_plan_version": "0.2.0",
        "transaction_id": "a" * 20,
        "master_index_sha256": sha(master_before),
        "canonical_write_performed": False,
        "items": [
            {
                "lane": "NEW",
                "action": "CREATE",
                "package_id": "d" * 20,
                "revision_key": "e" * 20,
                "record_id": "b" * 20,
                "target_path": new_target,
                "content_path": new_private,
                "content_sha256": sha(new_content),
            },
            {
                "lane": "UPDATE",
                "action": "UPDATE",
                "candidate_id": "f" * 20,
                "record_id": "c" * 20,
                "target_path": update_target,
                "content_path": update_private,
                "content_sha256": sha(update_content),
                "base_sha256": "1" * 64,
            },
        ],
    }
    content = {
        new_private: new_content,
        new_target: new_content,
        update_private: update_content,
        update_target: update_content,
    }
    reader = lambda relative: content.get(relative)

    receipt, errors = build_receipt(plan, master_before, master_after, reader)
    assert not errors, errors
    assert receipt is not None
    assert receipt["receipt_version"] == "0.2.0"
    assert receipt["state"] == "VERIFIED_COMMITTED_STATE"
    assert receipt["counts"] == {"items": 2, "create": 1, "update": 1, "unchanged": 0}
    assert receipt["master_index_changed"] is True
    assert receipt["candidate_settlement_eligible"] is True
    assert receipt["canonical_write_performed"] is False
    new_item = next(item for item in receipt["items"] if item["lane"] == "NEW")
    update_item = next(item for item in receipt["items"] if item["lane"] == "UPDATE")
    assert new_item["package_id"] == "d" * 20
    assert new_item["revision_key"] == "e" * 20
    assert update_item["candidate_id"] == "f" * 20

    content[update_target] = b"wrong bytes\n"
    blocked, blocked_errors = build_receipt(plan, master_before, master_after, reader)
    assert blocked is None
    assert "item_1_target_bytes_mismatch" in blocked_errors

    bad_master, bad_master_errors = build_receipt(plan, master_before + b"x", master_after, reader)
    assert bad_master is None
    assert "master_before_sha_mismatch" in bad_master_errors

    missing_provenance = dict(plan)
    missing_provenance["items"] = [dict(item) for item in plan["items"]]
    missing_provenance["items"][1].pop("candidate_id")
    blocked, blocked_errors = build_receipt(missing_provenance, master_before, master_after, reader)
    assert blocked is None
    assert "item_1_update_provenance_missing" in blocked_errors

    print(
        "candidate_transaction_receipt_smoke_ok target_bytes=1 master_binding=1 provenance=1 "
        "canonical_write=0"
    )


if __name__ == "__main__":
    main()
