#!/usr/bin/env python3
"""Smoke tests for live-revalidated private transaction preparation."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_finalize_batch import build_finalize_batch, verify_finalize_batch
from candidate_transaction_prepare import (
    _existing_conflict,
    parse_update_manifest,
    prepare_transaction,
)
from candidate_update_ready import build_ready_manifest
from library_index_build import load_records, planned_indexes, write_local_atomic


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        base_target = "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-existing.md"
        new_target = "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-new.md"
        new_content_path = "99_INBOX/CANDIDATES/PUBLISH_READY/records/new.md"
        update_content_path = "99_INBOX/CANDIDATES/UPDATE_READY/records/aaaaaaaaaaaaaaaaaaaa.md"

        base = """RECORD_ID: aaaaaaaaaaaaaaaaaaaa
TYPE: TECHNOLOGY
STATUS: TEST
DOMAIN: 04_AI_AGENTS
CATEGORY: INTEGRATIONS
TITLE: Existing

# Existing
""".encode("utf-8")
        updated = """RECORD_ID: aaaaaaaaaaaaaaaaaaaa
TYPE: TECHNOLOGY
STATUS: REFERENCE
DOMAIN: 04_AI_AGENTS
CATEGORY: INTEGRATIONS
TITLE: Existing

# Existing

## VERIFIED CAPABILITIES

- Existing now supports verified export.
""".encode("utf-8")
        new = """RECORD_ID: bbbbbbbbbbbbbbbbbbbb
TYPE: TECHNOLOGY
STATUS: TEST
DOMAIN: 04_AI_AGENTS
CATEGORY: INTEGRATIONS
TITLE: New

# New
""".encode("utf-8")

        for rel, content in (
            (base_target, base),
            (new_content_path, new),
            (update_content_path, updated),
        ):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        records = load_records(root, None)
        for rel, content in planned_indexes(records).items():
            write_local_atomic(root / rel, content)
        master = (root / "00_LIBRARY/MASTER_INDEX.md").read_bytes()

        manifest = {
            "schema_version": 1,
            "publish_version": "0.2.0",
            "items": [
                {
                    "package_id": "c" * 20,
                    "revision_key": "d" * 20,
                    "record_id": "b" * 20,
                    "record_type": "TECHNOLOGY",
                    "status": "TEST",
                    "domain": "04_AI_AGENTS",
                    "category": "INTEGRATIONS",
                    "slug": "new",
                    "title": "New",
                    "content_path": new_content_path,
                    "content_sha256": sha(new),
                }
            ],
        }
        validation = {
            "schema_version": 1,
            "validate_version": "0.1.0",
            "canonical_write_performed": False,
            "items": [
                {
                    "record_id": "b" * 20,
                    "record_type": "TECHNOLOGY",
                    "status": "TEST",
                    "title": "New",
                    "target_path": new_target,
                    "content_sha256": sha(new),
                    "action": "CREATE",
                }
            ],
            "counts": {"create": 1, "unchanged": 0},
        }
        update_artifact = {
            "schema_version": 1,
            "update_ready_version": "0.2.0",
            "merge_render_version": "0.1.0",
            "candidate_id": "e" * 20,
            "record_id": "a" * 20,
            "target_path": base_target,
            "base_sha256": sha(base),
            "merged_sha256": sha(updated),
            "content_path": update_content_path,
            "requires_base_sha_match_before_write": True,
            "canonical_write_performed": False,
        }

        update_manifest, manifest_errors = build_ready_manifest([update_artifact])
        assert not manifest_errors and update_manifest is not None
        selected_updates, select_errors = parse_update_manifest(update_manifest)
        assert not select_errors
        assert selected_updates == [update_artifact]

        manifest_raw = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        validation_raw = (json.dumps(validation, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        update_manifest_raw = (json.dumps(update_manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        batch, batch_errors = build_finalize_batch(
            master,
            manifest_raw,
            validation_raw,
            update_manifest_raw,
        )
        assert not batch_errors and batch is not None
        assert not verify_finalize_batch(
            batch,
            master,
            manifest_raw,
            validation_raw,
            update_manifest_raw,
        )

        reader = lambda relative: (root / relative).read_bytes() if (root / relative).exists() else None
        plan, errors = prepare_transaction(
            master_content=master,
            new_manifest=manifest,
            new_validation=validation,
            update_artifacts=selected_updates,
            read_content=reader,
            finalize_batch_id=batch["batch_id"],
        )
        assert not errors, errors
        assert plan is not None
        assert plan["prepare_version"] == "0.3.0"
        assert plan["finalize_batch_id"] == batch["batch_id"]
        assert plan["live_preconditions_verified"] is True
        assert plan["counts"]["create"] == 1
        assert plan["counts"]["update"] == 1
        assert plan["counts"]["writes"] == 2
        assert plan["canonical_write_performed"] is False
        assert plan["transaction_plan_version"] == "0.2.0"
        update_item = next(item for item in plan["items"] if item["lane"] == "UPDATE")
        assert update_item["candidate_id"] == "e" * 20
        new_item = next(item for item in plan["items"] if item["lane"] == "NEW")
        assert new_item["package_id"] == "c" * 20
        assert new_item["revision_key"] == "d" * 20

        same_bytes = (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        assert _existing_conflict(None, plan) is False
        assert _existing_conflict(same_bytes, plan) is False
        other = dict(plan)
        other["finalize_batch_id"] = "f" * 20
        other_bytes = (json.dumps(other, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        assert _existing_conflict(other_bytes, plan) is True

        # Any concurrent canonical update invalidates private transaction prep.
        (root / base_target).write_bytes(base + b"\n")
        stale_plan, stale_errors = prepare_transaction(
            master_content=master,
            new_manifest=manifest,
            new_validation=validation,
            update_artifacts=selected_updates,
            read_content=reader,
            finalize_batch_id=batch["batch_id"],
        )
        assert stale_plan is None
        assert "update_0_base_sha_changed" in stale_errors

        # A candidate payload changing after dry-run must also fail closed.
        (root / base_target).write_bytes(base)
        (root / new_content_path).write_bytes(new + b"\n")
        changed_plan, changed_errors = prepare_transaction(
            master_content=master,
            new_manifest=manifest,
            new_validation=validation,
            update_artifacts=selected_updates,
            read_content=reader,
            finalize_batch_id=batch["batch_id"],
        )
        assert changed_plan is None
        assert "new_0_content_sha_changed" in changed_errors

        # Torn/mixed finalizer state is rejected by the batch barrier itself.
        torn_errors = verify_finalize_batch(
            batch,
            master,
            manifest_raw,
            validation_raw + b" ",
            update_manifest_raw,
        )
        assert "finalize_batch_publish_validation_sha256_mismatch" in torn_errors

    print(
        "candidate_transaction_prepare_smoke_ok live_new=1 live_update=1 "
        "batch_bound=1 torn_state_blocked=1 transaction_conflict=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
