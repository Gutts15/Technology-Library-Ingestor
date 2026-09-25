#!/usr/bin/env python3
"""Dependency-free smoke tests for the non-publishing transaction planner."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_transaction_plan import build_transaction_plan


def main() -> None:
    master = b"# MASTER_INDEX\n\nGENERATED: TRUE\nRECORDS: 1\n"
    new_item = {
        "package_id": "a" * 20,
        "revision_key": "b" * 20,
        "record_id": "c" * 20,
        "record_type": "TECHNOLOGY",
        "status": "TEST",
        "domain": "04_AI_AGENTS",
        "category": "INTEGRATIONS",
        "slug": "new-agent-bridge",
        "title": "New Agent Bridge",
        "content_path": "99_INBOX/CANDIDATES/PUBLISH_READY/records/cccccccccccccccccccc.md",
        "content_sha256": "d" * 64,
    }
    manifest = {
        "schema_version": 1,
        "publish_version": "0.2.0",
        "items": [new_item],
    }
    validation = {
        "schema_version": 1,
        "validate_version": "0.1.0",
        "canonical_write_performed": False,
        "items": [
            {
                "record_id": "c" * 20,
                "record_type": "TECHNOLOGY",
                "status": "TEST",
                "title": "New Agent Bridge",
                "target_path": "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-new-agent-bridge.md",
                "content_sha256": "d" * 64,
                "action": "CREATE",
            }
        ],
        "counts": {"create": 1, "unchanged": 0},
    }
    update = {
        "schema_version": 1,
        "update_ready_version": "0.2.0",
        "merge_render_version": "0.1.0",
        "candidate_id": "9" * 20,
        "target_path": "00_LIBRARY/04_AI_AGENTS/WORKFLOWS/technology-existing-tool.md",
        "record_id": "e" * 20,
        "base_sha256": "f" * 64,
        "merged_sha256": "1" * 64,
        "proposed_status": "REFERENCE",
        "claims_input": 2,
        "claims_added": 2,
        "claims_deduped": 0,
        "content_path": "99_INBOX/CANDIDATES/UPDATE_READY/records/eeeeeeeeeeeeeeeeeeee.md",
        "requires_base_sha_match_before_write": True,
        "canonical_write_performed": False,
    }

    plan, errors = build_transaction_plan(master, manifest, validation, [update])
    assert not errors, errors
    assert plan is not None
    assert plan["transaction_plan_version"] == "0.2.0"
    assert plan["counts"] == {"create": 1, "update": 1, "unchanged": 0, "writes": 2}
    assert plan["master_index_sha256"] == hashlib.sha256(master).hexdigest()
    assert plan["requires_live_revalidation"] is True
    assert plan["requires_index_rebuild_after_write"] is True
    assert plan["requires_rollback_on_partial_failure"] is True
    assert plan["canonical_write_performed"] is False
    assert {item["lane"] for item in plan["items"]} == {"NEW", "UPDATE"}
    new_plan_item = next(item for item in plan["items"] if item["lane"] == "NEW")
    update_plan_item = next(item for item in plan["items"] if item["lane"] == "UPDATE")
    assert new_plan_item["package_id"] == "a" * 20
    assert new_plan_item["revision_key"] == "b" * 20
    assert update_plan_item["candidate_id"] == "9" * 20

    # Same target across lanes must fail before any future publisher can see it.
    colliding_update = dict(update)
    colliding_update["target_path"] = validation["items"][0]["target_path"]
    blocked, blocked_errors = build_transaction_plan(master, manifest, validation, [colliding_update])
    assert blocked is None
    assert any("target_collision" in error for error in blocked_errors)

    # A stale/changed validation plan cannot silently drift from its manifest.
    mismatched_validation = dict(validation)
    mismatched_validation["items"] = [dict(validation["items"][0])]
    mismatched_validation["items"][0]["content_sha256"] = "2" * 64
    blocked, blocked_errors = build_transaction_plan(master, manifest, mismatched_validation, [])
    assert blocked is None
    assert "new_item_0_sha" in blocked_errors

    # Empty transaction is valid and explicitly requires no write/index work.
    empty, empty_errors = build_transaction_plan(master, None, None, [])
    assert not empty_errors, empty_errors
    assert empty is not None
    assert empty["counts"]["writes"] == 0
    assert empty["requires_index_rebuild_after_write"] is False

    print(
        "candidate_transaction_plan_smoke_ok new=1 update=1 provenance=1 collision_guard=1 "
        "master_sha_bound=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
