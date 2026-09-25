#!/usr/bin/env python3
"""Dependency-free split planning smoke tests."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_queue import validate_candidate
from candidate_split_plan import build_split_plan, parse_split_payload


def fixture() -> tuple[str, dict]:
    candidate = """TYPE: CANDIDATE
CANDIDATE_STATUS: TO_REVIEW
PROPOSED_TYPE: TECHNOLOGY
PROPOSED_STATUS: TEST
PROPOSED_DOMAIN: 04_AI_AGENTS
PROPOSED_CATEGORY: INTEGRATIONS
TITLE: Broad Agent Engine Research
LAST_CHECKED: 2026-09-10
SOURCE_ORIGIN: Synthetic test.

# Broad Agent Engine Research

This bundle discusses Tool Alpha and Tool Beta.
"""
    sha = hashlib.sha256(candidate.encode()).hexdigest()
    review = {
        "schema_version": 1,
        "candidate_id": sha[:20],
        "candidate_sha256": sha,
        "final_review": {
            "decision": "SPLIT_REQUIRED",
            "evidence_level": "HIGH",
            "proposed_type": None,
            "proposed_status": None,
            "canonical_match_path": None,
            "rationale": "Tool Alpha and Tool Beta are independently maintainable subjects.",
        },
        "source_probe": {
            "schema_version": 1,
            "sources": [
                {
                    "status": "OK",
                    "source_url": "https://example.com/alpha",
                    "excerpt": "Tool Alpha controls an editor.",
                },
                {
                    "status": "OK",
                    "source_url": "https://example.com/beta",
                    "excerpt": "Tool Beta automates builds.",
                },
                {
                    "status": "ERROR",
                    "source_url": "https://example.com/failed",
                    "excerpt": "",
                },
            ],
        },
    }
    return candidate, review


def main() -> None:
    candidate, review = fixture()
    payload = {
        "children": [
            {
                "title": "Tool Alpha",
                "proposed_type": "TECHNOLOGY",
                "proposed_status": "TEST",
                "proposed_domain": "04_AI_AGENTS",
                "proposed_category": "INTEGRATIONS",
                "summary": "Tool Alpha exposes editor-control capabilities.",
                "source_indices": [1],
            },
            {
                "title": "Tool Beta",
                "proposed_type": "TECHNOLOGY",
                "proposed_status": "TEST",
                "proposed_domain": "04_AI_AGENTS",
                "proposed_category": "INTEGRATIONS",
                "summary": "Tool Beta provides build automation capabilities.",
                "source_indices": [2],
            },
        ]
    }

    parsed, parse_errors = parse_split_payload(
        payload,
        parent_title="Broad Agent Engine Research",
        probe=review["source_probe"],
    )
    assert not parse_errors, parse_errors
    assert len(parsed) == 2

    plan, errors = build_split_plan(candidate, review, payload)
    assert not errors, errors
    assert plan is not None
    assert plan["child_count"] == 2
    assert plan["candidate_write_performed"] is False
    assert plan["canonical_write_performed"] is False
    assert plan["paid_model_used"] is False
    assert {child["filename"] for child in plan["children"]} == {
        "candidate-tool-alpha.md",
        "candidate-tool-beta.md",
    }
    for child in plan["children"]:
        headers, candidate_errors = validate_candidate(child["filename"], child["markdown"])
        assert not candidate_errors, candidate_errors
        assert headers["CANDIDATE_STATUS"] == "TO_REVIEW"
        assert "SPLIT_PARENT_ID:" in child["markdown"]
        assert "must pass the normal candidate validation" in child["markdown"]

    failed_source_payload = {
        "children": [payload["children"][0], {**payload["children"][1], "source_indices": [3]}]
    }
    bad, bad_errors = build_split_plan(candidate, review, failed_source_payload)
    assert bad is None
    assert "child_1_source_without_success" in bad_errors

    same_title_payload = {
        "children": [
            {**payload["children"][0], "title": "Broad Agent Engine Research"},
            payload["children"][1],
        ]
    }
    same, same_errors = build_split_plan(candidate, review, same_title_payload)
    assert same is None
    assert "child_0_not_narrower" in same_errors

    stale_review = dict(review)
    stale_review["candidate_sha256"] = "0" * 64
    stale, stale_errors = build_split_plan(candidate, stale_review, payload)
    assert stale is None
    assert "semantic_candidate_sha_mismatch" in stale_errors

    print("candidate_split_plan_smoke_ok children=2 candidate_write=0 canonical_write=0 paid_model=0")


if __name__ == "__main__":
    main()
