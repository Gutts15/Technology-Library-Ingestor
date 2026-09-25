#!/usr/bin/env python3
"""Smoke tests for the non-publishing VALIDATED_UPDATE plan contract."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_update_plan import build_update_plan


def main() -> None:
    target = "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-synthetic-agent-bridge.md"
    base = """RECORD_ID: dddddddddddddddddddd
TYPE: TECHNOLOGY
STATUS: TEST
DOMAIN: 04_AI_AGENTS
CATEGORY: INTEGRATIONS
TITLE: Synthetic Agent Bridge

# Synthetic Agent Bridge

Existing stable canonical guidance.
""".encode("utf-8")

    candidate_id = "a" * 20
    candidate_sha = "b" * 64
    package = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "candidate_sha256": candidate_sha,
        "package_id": "c" * 20,
        "revision_key": "e" * 20,
        "decision": "VALIDATED_UPDATE",
        "requires_existing_record_merge": True,
        "canonical_write_performed": False,
        "proposed_record": {
            "decision": "VALIDATED_UPDATE",
            "evidence_level": "HIGH",
            "record_type": "TECHNOLOGY",
            "status": "REFERENCE",
            "domain": "04_AI_AGENTS",
            "category": "INTEGRATIONS",
            "slug": "synthetic-agent-bridge",
            "title": "Synthetic Agent Bridge",
            "target_path": target,
            "canonical_match_path": target,
            "record_id": None,
        },
    }
    claim_review = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "candidate_sha256": candidate_sha,
        "state": "READY",
        "publication_claims": [
            {
                "claim": "Synthetic Agent Bridge now supports a verified capability.",
                "support": "SUPPORTED",
                "source_indices": [1],
            },
            {
                "claim": "The repository has 99 stars.",
                "support": "SUPPORTED",
                "source_indices": [1],
            },
        ],
    }
    probe = {
        "schema_version": 1,
        "sources": [
            {
                "status": "OK",
                "source_url": "https://example.com/official",
                "excerpt": "Synthetic Agent Bridge now supports a verified capability.",
            }
        ],
    }

    plan, errors = build_update_plan(package, claim_review, probe, base, target)
    assert not errors, errors
    assert plan is not None
    assert plan["base_record"]["record_id"] == "d" * 20
    assert plan["base_record"]["sha256"] == hashlib.sha256(base).hexdigest()
    assert plan["base_record"]["path"] == target
    assert plan["proposed_status"] == "REFERENCE"
    assert plan["update_plan_version"] == "0.2.0"
    assert plan["editorial_dropped_claims"] == 1
    assert len(plan["supported_claims"]) == 1
    assert plan["supported_claims"][0]["editorial_kind"] == "CAPABILITY"
    assert plan["supported_claims"][0]["source_refs"][0]["source_url"] == "https://example.com/official"
    assert plan["requires_merge_render"] is True
    assert plan["requires_base_sha_match_before_write"] is True
    assert plan["canonical_write_performed"] is False

    # Existing technologies may legitimately receive a compatibility-only update.
    # The base record already establishes that the technology has functionality,
    # so the new-record functional-minimum gate must not be re-imposed here.
    compatibility_review = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "candidate_sha256": candidate_sha,
        "state": "READY",
        "publication_claims": [
            {
                "claim": "Synthetic Agent Bridge is compatible with Godot 4.5.",
                "support": "SUPPORTED",
                "source_indices": [1],
            }
        ],
    }
    compatibility_plan, compatibility_errors = build_update_plan(
        package, compatibility_review, probe, base, target
    )
    assert not compatibility_errors, compatibility_errors
    assert compatibility_plan is not None
    assert len(compatibility_plan["supported_claims"]) == 1
    assert compatibility_plan["supported_claims"][0]["editorial_kind"] == "COMPATIBILITY"

    metadata_only_review = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "candidate_sha256": candidate_sha,
        "state": "READY",
        "publication_claims": [
            {
                "claim": "The repository has 100 stars.",
                "support": "SUPPORTED",
                "source_indices": [1],
            }
        ],
    }
    metadata_plan, metadata_errors = build_update_plan(
        package, metadata_only_review, probe, base, target
    )
    assert metadata_plan is None
    assert "editorial_no_canonical_claims" in metadata_errors

    wrong_path_package = dict(package)
    wrong_path_package["proposed_record"] = dict(package["proposed_record"])
    wrong_path_package["proposed_record"]["canonical_match_path"] = "00_LIBRARY/other.md"
    wrong_plan, wrong_errors = build_update_plan(
        wrong_path_package, claim_review, probe, base, target
    )
    assert wrong_plan is None
    assert "canonical_match_path_mismatch" in wrong_errors

    invented_id_package = dict(package)
    invented_id_package["proposed_record"] = dict(package["proposed_record"])
    invented_id_package["proposed_record"]["record_id"] = "f" * 20
    invented_plan, invented_errors = build_update_plan(
        invented_id_package, claim_review, probe, base, target
    )
    assert invented_plan is None
    assert "update_must_not_invent_record_id" in invented_errors

    mismatched_title_package = dict(package)
    mismatched_title_package["proposed_record"] = dict(package["proposed_record"])
    mismatched_title_package["proposed_record"]["title"] = "Different Canonical Identity"
    mismatch_plan, mismatch_errors = build_update_plan(
        mismatched_title_package, claim_review, probe, base, target
    )
    assert mismatch_plan is None
    assert "title_identity_mismatch" in mismatch_errors

    failed_probe = {
        "schema_version": 1,
        "sources": [
            {
                "status": "ERROR",
                "source_url": "https://example.com/official",
                "excerpt": "",
            }
        ],
    }
    failed_plan, failed_errors = build_update_plan(
        package, claim_review, failed_probe, base, target
    )
    assert failed_plan is None
    assert "successful_sources_missing" in failed_errors
    assert "claim_0_source_not_successful" in failed_errors

    print(
        "candidate_update_plan_ok base_sha_bound=1 supported_only=1 editorial_filter=1 "
        "compatibility_only_update=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
