#!/usr/bin/env python3
"""Smoke tests for sealed candidate batch inspection."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_batch_inspect import inspect_batch, render_markdown
from candidate_finalize_batch import build_finalize_batch
from candidate_update_ready import UPDATE_READY_VERSION


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_new() -> bytes:
    return """RECORD_ID: bbbbbbbbbbbbbbbbbbbb
TYPE: TECHNOLOGY
STATUS: TEST
DOMAIN: 04_AI_AGENTS
CATEGORY: INTEGRATIONS
TITLE: New Bridge
COST: Not established by validated evidence.
LICENSE: MIT.
COMPATIBILITY: Works with Example Editor.
LAST_CHECKED: 2026-09-12
SOURCE: fixture

# New Bridge

## SUMMARY

- Controls the editor through a local bridge. [S1]

## REFERENCES

- S1: https://example.com/new-bridge
""".encode("utf-8")


def canonical_base() -> bytes:
    return """RECORD_ID: aaaaaaaaaaaaaaaaaaaa
TYPE: TECHNOLOGY
STATUS: TEST
DOMAIN: 04_AI_AGENTS
CATEGORY: INTEGRATIONS
TITLE: Existing Bridge

# Existing Bridge

## SUMMARY

Original state.
""".encode("utf-8")


def canonical_update() -> bytes:
    return """RECORD_ID: aaaaaaaaaaaaaaaaaaaa
TYPE: TECHNOLOGY
STATUS: REFERENCE
DOMAIN: 04_AI_AGENTS
CATEGORY: INTEGRATIONS
TITLE: Existing Bridge

# Existing Bridge

## SUMMARY

Original state.

## VERIFIED CAPABILITIES

- Adds a verified capability. [S1]
""".encode("utf-8")


def encode(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build_inputs() -> tuple[dict[str, bytes | None], dict[str, bytes]]:
    new = canonical_new()
    base = canonical_base()
    update = canonical_update()
    new_path = "99_INBOX/CANDIDATES/PUBLISH_READY/records/bbbbbbbbbbbbbbbbbbbb.md"
    update_path = "99_INBOX/CANDIDATES/UPDATE_READY/records/aaaaaaaaaaaaaaaaaaaa.md"
    new_target = "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-new-bridge.md"
    update_target = "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-existing-bridge.md"

    master = """# MASTER_INDEX

GENERATED: TRUE
RECORDS: 1

## 04_AI_AGENTS

### INTEGRATIONS

- TECHNOLOGY - Existing Bridge [TEST] (`00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-existing-bridge.md`)
""".encode("utf-8")
    publish = {
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
                "slug": "new-bridge",
                "title": "New Bridge",
                "content_path": new_path,
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
                "title": "New Bridge",
                "target_path": new_target,
                "content_sha256": sha(new),
                "action": "CREATE",
            }
        ],
        "counts": {"create": 1, "unchanged": 0},
    }
    update_manifest = {
        "schema_version": 1,
        "update_ready_version": UPDATE_READY_VERSION,
        "canonical_write_performed": False,
        "items": [
            {
                "schema_version": 1,
                "update_ready_version": UPDATE_READY_VERSION,
                "merge_render_version": "0.1.0",
                "candidate_id": "e" * 20,
                "record_id": "a" * 20,
                "target_path": update_target,
                "base_sha256": sha(base),
                "merged_sha256": sha(update),
                "content_path": update_path,
                "proposed_status": "REFERENCE",
                "claims_input": 2,
                "claims_added": 1,
                "claims_deduped": 1,
                "requires_base_sha_match_before_write": True,
                "canonical_write_performed": False,
            }
        ],
    }
    publish_raw = encode(publish)
    validation_raw = encode(validation)
    update_raw = encode(update_manifest)
    batch, batch_errors = build_finalize_batch(master, publish_raw, validation_raw, update_raw)
    assert not batch_errors and batch is not None

    controls = {
        "master": master,
        "publish": publish_raw,
        "validation": validation_raw,
        "update": update_raw,
        "batch": encode(batch),
    }
    content_map: dict[str, bytes | None] = {
        new_path: new,
        update_path: update,
        new_target: None,
        update_target: base,
    }
    return content_map, controls


def main() -> None:
    content_map, controls = build_inputs()
    report, errors = inspect_batch(
        master_raw=controls["master"],
        publish_raw=controls["publish"],
        validation_raw=controls["validation"],
        update_raw=controls["update"],
        batch_raw=controls["batch"],
        read_content=lambda path: content_map.get(path),
    )
    assert not errors, errors
    assert report is not None
    assert report["state"] == "READY_FOR_EDITORIAL_REVIEW"
    assert report["counts"] == {
        "items": 2,
        "new_lane": 1,
        "update_lane": 1,
        "create": 1,
        "unchanged": 0,
        "update": 1,
    }
    assert report["current_library_record_count"] == 1
    assert report["expected_library_record_count_after_commit"] == 2
    assert report["live_preconditions_verified"] is True
    assert report["canonical_write_performed"] is False
    assert {item["title"] for item in report["items"]} == {"New Bridge", "Existing Bridge"}
    markdown = render_markdown(report)
    assert "New Bridge" in markdown
    assert "Existing Bridge" in markdown
    assert "CANONICAL_WRITE_PERFORMED: FALSE" in markdown

    # A live CREATE collision must invalidate the report instead of merely warning.
    stale_map = dict(content_map)
    stale_map["00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-new-bridge.md"] = canonical_new()
    stale_report, stale_errors = inspect_batch(
        master_raw=controls["master"],
        publish_raw=controls["publish"],
        validation_raw=controls["validation"],
        update_raw=controls["update"],
        batch_raw=controls["batch"],
        read_content=lambda path: stale_map.get(path),
    )
    assert stale_report is None
    assert stale_errors == ["new_0_create_target_exists"]

    # Any mixed control-file bytes must fail at the sealed FINALIZE_BATCH barrier.
    torn_validation = controls["validation"] + b"\n"
    torn_report, torn_errors = inspect_batch(
        master_raw=controls["master"],
        publish_raw=controls["publish"],
        validation_raw=torn_validation,
        update_raw=controls["update"],
        batch_raw=controls["batch"],
        read_content=lambda path: content_map.get(path),
    )
    assert torn_report is None
    assert "finalize_batch_publish_validation_sha256_mismatch" in torn_errors

    print(
        "candidate_batch_inspect_smoke_ok sealed_batch=1 new_update=1 live_preconditions=1 "
        "operator_report=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
