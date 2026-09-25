#!/usr/bin/env python3
"""Dependency-free smoke tests for isolated candidate UPDATE_READY staging."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_update_ready import build_ready_artifact, build_ready_manifest, write_local_ready


def make_base() -> tuple[str, bytes]:
    target = "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-synthetic-agent-bridge.md"
    base = """RECORD_ID: dddddddddddddddddddd
TYPE: TECHNOLOGY
STATUS: TEST
DOMAIN: 04_AI_AGENTS
CATEGORY: INTEGRATIONS
TITLE: Synthetic Agent Bridge

# Synthetic Agent Bridge

## SUMMARY

Stable summary.
""".encode("utf-8")
    return target, base


def make_plan(target: str, base: bytes) -> dict:
    return {
        "schema_version": 1,
        "update_plan_version": "0.1.0",
        "candidate_id": "a" * 20,
        "candidate_sha256": "b" * 64,
        "package_id": "c" * 20,
        "revision_key": "e" * 20,
        "base_record": {
            "record_id": "d" * 20,
            "record_type": "TECHNOLOGY",
            "status": "TEST",
            "domain": "04_AI_AGENTS",
            "category": "INTEGRATIONS",
            "title": "Synthetic Agent Bridge",
            "path": target,
            "sha256": hashlib.sha256(base).hexdigest(),
        },
        "proposed_status": "REFERENCE",
        "editorial_gate_version": "0.1.2",
        "editorial_dropped_claims": 0,
        "supported_claims": [
            {
                "claim": "Synthetic Agent Bridge can export validated project data.",
                "editorial_kind": "CAPABILITY",
                "source_refs": [
                    {
                        "source_index": 1,
                        "source_url": "https://example.com/new",
                        "source_url_sha256": hashlib.sha256(b"https://example.com/new").hexdigest(),
                    }
                ],
            }
        ],
        "requires_merge_render": True,
        "requires_base_sha_match_before_write": True,
        "canonical_write_performed": False,
    }


def main() -> None:
    target, base = make_base()
    plan = make_plan(target, base)
    content, artifact, errors = build_ready_artifact(plan, base)
    assert not errors, errors
    assert content is not None and artifact is not None
    assert artifact["candidate_id"] == "a" * 20
    assert artifact["record_id"] == "d" * 20
    assert artifact["target_path"] == target
    assert artifact["base_sha256"] == hashlib.sha256(base).hexdigest()
    assert artifact["merged_sha256"] == hashlib.sha256(content).hexdigest()
    assert artifact["content_path"].startswith(
        "99_INBOX/CANDIDATES/UPDATE_READY/records/"
    )
    assert artifact["update_ready_version"] == "0.2.0"
    assert artifact["canonical_write_performed"] is False

    latest, latest_errors = build_ready_manifest([artifact])
    assert not latest_errors, latest_errors
    assert latest is not None
    assert latest["update_ready_version"] == "0.2.0"
    assert len(latest["items"]) == 1
    assert latest["items"][0]["candidate_id"] == "a" * 20

    empty, empty_errors = build_ready_manifest([])
    assert not empty_errors
    assert empty is not None and empty["items"] == []

    duplicate, duplicate_errors = build_ready_manifest([artifact, dict(artifact)])
    assert duplicate is None
    assert any("duplicate_candidate" in error for error in duplicate_errors)

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        content_path, metadata_path = write_local_ready(root, content, artifact)
        assert content_path.read_bytes() == content
        assert metadata_path.exists()
        assert "00_LIBRARY" not in content_path.parts

    changed = base + b"\n"
    blocked_content, blocked_artifact, blocked_errors = build_ready_artifact(plan, changed)
    assert blocked_content is None and blocked_artifact is None
    assert "base_sha_changed" in blocked_errors

    print(
        "candidate_update_ready_smoke_ok isolated=1 base_sha_bound=1 current_batch=1 "
        "canonical_write=0"
    )


if __name__ == "__main__":
    main()
