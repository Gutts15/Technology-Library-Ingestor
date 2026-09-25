#!/usr/bin/env python3
"""Dependency-free smoke tests for the candidate semantic/curation/publish boundary."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_canonical_draft import build_markdown
from candidate_claim_review import build_review, parse_claim_payload
from candidate_curation_prepare import build_draft, canonical_target, source_category_from_domain
from candidate_publish_index import merge_manifests, parse_candidate_manifest
from candidate_publish_prepare import build_content, build_manifest
from candidate_publish_validate import assess_publication
from library_publish import parse_manifest


def candidate_fixture() -> tuple[str, str, dict, str]:
    candidate = """TYPE: CANDIDATE
CANDIDATE_STATUS: TO_REVIEW
PROPOSED_TYPE: TECHNOLOGY
PROPOSED_STATUS: TEST
PROPOSED_DOMAIN: 04_AI_AGENTS
PROPOSED_CATEGORY: INTEGRATIONS
TITLE: Synthetic Agent Bridge
LAST_CHECKED: 2026-09-10
SOURCE_ORIGIN: Synthetic test.

# Synthetic Agent Bridge

Reusable technical candidate material.
"""
    sha = hashlib.sha256(candidate.encode()).hexdigest()
    candidate_id = sha[:20]
    semantic_review = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "candidate_sha256": sha,
        "canonical_write_performed": False,
        "final_review": {
            "decision": "VALIDATED_NEW",
            "evidence_level": "HIGH",
            "proposed_type": "TECHNOLOGY",
            "proposed_status": "TEST",
            "canonical_match_path": None,
            "rationale": "Synthetic evidence supports a reusable integration.",
        },
    }
    master = """# MASTER_INDEX

## 04_AI_AGENTS
### INTEGRATIONS
- TECHNOLOGY - Existing Tool [REFERENCE] (`00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-existing-tool.md`)
"""
    return candidate, candidate_id, semantic_review, master


def main() -> None:
    candidate, candidate_id, semantic_review, master = candidate_fixture()

    package, material, errors = build_draft(candidate, semantic_review, master)
    assert not errors, errors
    assert package is not None and material is not None
    assert package["publishable"] is False
    assert package["canonical_write_performed"] is False
    assert package["proposed_record"]["record_type"] == "TECHNOLOGY"
    target = "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-synthetic-agent-bridge.md"
    assert package["proposed_record"]["target_path"] == target
    assert package["proposed_record"]["record_id"]
    assert material.startswith("DRAFT_ONLY: TRUE")
    assert not any(line.startswith("RECORD_ID:") for line in material.splitlines())

    colliding = master + f"\n- TECHNOLOGY - Synthetic Agent Bridge [TEST] (`{target}`)\n"
    collision_package, _, collision_errors = build_draft(candidate, semantic_review, colliding)
    assert collision_package is None
    assert "new_record_title_collision" in collision_errors

    source_review = dict(semantic_review)
    source_review["final_review"] = dict(semantic_review["final_review"])
    source_review["final_review"]["decision"] = "SOURCE_ONLY"
    source_review["final_review"]["evidence_level"] = "LOW"
    source_package, _, source_errors = build_draft(candidate, source_review, master)
    assert not source_errors, source_errors
    assert source_package is not None
    assert source_package["proposed_record"]["record_type"] == "SOURCE"
    assert source_package["proposed_record"]["domain"] == "SOURCES"
    assert source_package["proposed_record"]["category"] == "AI_AGENTS"
    assert source_package["proposed_record"]["status"] == "TEST"

    probe = {
        "schema_version": 1,
        "generated_at": "2026-09-10T18:00:00+00:00",
        "sources": [
            {
                "status": "OK",
                "source_url": "https://example.com/official",
                "excerpt": "Agent Bridge supports editor control.",
            },
            {
                "status": "ERROR",
                "source_url": "https://example.com/failed",
                "excerpt": "",
            },
        ],
    }
    model_claims = {
        "claims": [
            {
                "claim": "Agent Bridge supports editor control.",
                "support": "SUPPORTED",
                "source_indices": [1],
            },
            {
                "claim": "Agent Bridge is perfect for every project.",
                "support": "UNSUPPORTED",
                "source_indices": [],
            },
        ]
    }
    parsed, parse_errors = parse_claim_payload(model_claims, probe)
    assert not parse_errors, parse_errors
    assert len(parsed) == 2

    claim_review, claim_errors = build_review(candidate, probe, model_claims)
    assert not claim_errors, claim_errors
    assert claim_review["state"] == "READY"
    assert claim_review["counts"]["supported"] == 1
    assert claim_review["counts"]["unsupported"] == 1
    assert len(claim_review["publication_claims"]) == 1

    canonical_draft, canonical_meta, canonical_errors = build_markdown(package, claim_review, probe)
    assert not canonical_errors, canonical_errors
    assert canonical_draft is not None and canonical_meta is not None
    assert canonical_meta["publishable"] is False
    assert canonical_meta["canonical_write_performed"] is False
    assert canonical_meta["supported_claims"] == 1
    assert canonical_draft.startswith("DRAFT_ONLY: TRUE")
    assert "Agent Bridge supports editor control." in canonical_draft
    assert "Agent Bridge is perfect for every project." not in canonical_draft
    assert not any(line.startswith("RECORD_ID:") for line in canonical_draft.splitlines())
    assert any(line.startswith("PROPOSED_RECORD_ID:") for line in canonical_draft.splitlines())

    publish_content, publish_item, publish_errors = build_content(package, claim_review, probe)
    assert not publish_errors, publish_errors
    assert publish_content is not None and publish_item is not None
    assert b"RECORD_ID:" in publish_content
    assert b"Agent Bridge supports editor control." in publish_content
    assert b"Agent Bridge is perfect for every project." not in publish_content
    assert publish_item["content_path"].startswith("99_INBOX/CANDIDATES/PUBLISH_READY/records/")

    manifest, manifest_errors = build_manifest([publish_item])
    assert not manifest_errors, manifest_errors
    assert manifest is not None and manifest["publish_version"] == "0.2.0"
    parsed_candidate_manifest = parse_candidate_manifest(manifest)
    assert parsed_candidate_manifest == manifest["items"]

    # The legacy canonical publisher must still reject the isolated candidate
    # prefix. Candidate publication gets its own explicit gate.
    try:
        parse_manifest(manifest)
    except ValueError as exc:
        assert str(exc) == "record_content_path", exc
    else:
        raise AssertionError("canonical publisher unexpectedly accepted candidate outbox")

    merged = merge_manifests([manifest])
    assert merged == manifest
    try:
        merge_manifests([manifest, manifest])
    except ValueError as exc:
        assert str(exc) == "aggregate_duplicate_record_id", exc
    else:
        raise AssertionError("duplicate aggregate unexpectedly accepted")

    contents = {publish_item["content_path"]: publish_content}
    existing = {target: None}
    create_plan, create_errors = assess_publication(manifest["items"], contents, existing, master)
    assert not create_errors, create_errors
    assert create_plan is not None
    assert create_plan["counts"] == {"create": 1, "unchanged": 0}
    assert create_plan["items"][0]["action"] == "CREATE"
    assert create_plan["canonical_write_performed"] is False

    record_id = publish_item["record_id"]
    indexed_master = master + f"\n- TECHNOLOGY - Synthetic Agent Bridge [TEST] (`{target}`)\n"
    unchanged_plan, unchanged_errors = assess_publication(
        manifest["items"], contents, {target: publish_content}, indexed_master
    )
    assert not unchanged_errors, unchanged_errors
    assert unchanged_plan is not None
    assert unchanged_plan["counts"] == {"create": 0, "unchanged": 1}

    changed = publish_content + b"\n"
    changed_item = dict(publish_item)
    changed_item["content_sha256"] = hashlib.sha256(changed).hexdigest()
    changed_contents = {changed_item["content_path"]: changed}
    blocked_plan, blocked_errors = assess_publication(
        [changed_item], changed_contents, {target: publish_content}, indexed_master
    )
    assert blocked_plan is None
    assert "item_0_same_owner_change_requires_validated_update" in blocked_errors
    assert record_id == publish_item["record_id"]

    bad_claims = {
        "claims": [
            {
                "claim": "Unsupported source mapping.",
                "support": "SUPPORTED",
                "source_indices": [2],
            }
        ]
    }
    _, bad_errors = parse_claim_payload(bad_claims, probe)
    assert "claim_0_support_without_source" in bad_errors

    conflict_claims = {
        "claims": [
            {"claim": "Conflicting claim.", "support": "CONFLICT", "source_indices": [1]}
        ]
    }
    conflict_review, conflict_errors = build_review(candidate, probe, conflict_claims)
    assert not conflict_errors, conflict_errors
    assert conflict_review["state"] == "NEEDS_REVIEW"
    blocked_draft, _, blocked_draft_errors = build_markdown(package, conflict_review, probe)
    assert blocked_draft is None
    assert "claim_review_not_ready" in blocked_draft_errors
    blocked_content, _, blocked_publish_errors = build_content(package, conflict_review, probe)
    assert blocked_content is None
    assert "claim_review_not_ready" in blocked_publish_errors

    assert source_category_from_domain("04_AI_AGENTS") == "AI_AGENTS"
    assert canonical_target("SOURCE", "SOURCES", "AI_AGENTS", "abc") == "00_LIBRARY/SOURCES/source-abc.md"

    print(
        "candidate_pipeline_smoke_ok "
        "curation=1 claim_review=1 supported_only=1 aggregate=1 dry_run_publish_gate=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
