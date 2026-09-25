#!/usr/bin/env python3
"""Dependency-free smoke tests for controlled split envelopes."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_split_controlled import (
    CONTROLLED_SPLIT_VERSION,
    build_envelope,
    plan_path_for,
    validate_envelope,
)
from candidate_split_plan import build_split_plan


def fixture() -> tuple[str, dict, dict]:
    candidate = """TYPE: CANDIDATE
CANDIDATE_STATUS: TO_REVIEW
PROPOSED_TYPE: TECHNOLOGY
PROPOSED_STATUS: TEST
PROPOSED_DOMAIN: 04_AI_AGENTS
PROPOSED_CATEGORY: INTEGRATIONS
TITLE: Broad Tool Bundle
LAST_CHECKED: 2026-09-11
SOURCE_ORIGIN: Synthetic test.

# Broad Tool Bundle

Tool Alpha and Tool Beta are independent.
"""
    sha = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
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
            "rationale": "Two independently maintainable tools.",
        },
        "source_probe": {
            "schema_version": 1,
            "sources": [
                {"status": "OK", "source_url": "https://example.com/a", "excerpt": "Tool Alpha."},
                {"status": "OK", "source_url": "https://example.com/b", "excerpt": "Tool Beta."},
            ],
        },
    }
    split_payload = {
        "children": [
            {
                "title": "Tool Alpha",
                "proposed_type": "TECHNOLOGY",
                "proposed_status": "TEST",
                "proposed_domain": "04_AI_AGENTS",
                "proposed_category": "INTEGRATIONS",
                "summary": "Tool Alpha has an independent lifecycle.",
                "source_indices": [1],
            },
            {
                "title": "Tool Beta",
                "proposed_type": "TECHNOLOGY",
                "proposed_status": "TEST",
                "proposed_domain": "04_AI_AGENTS",
                "proposed_category": "INTEGRATIONS",
                "summary": "Tool Beta has an independent lifecycle.",
                "source_indices": [2],
            },
        ]
    }
    return candidate, review, split_payload


def main() -> None:
    candidate, review, split_payload = fixture()
    split_plan, split_errors = build_split_plan(candidate, review, split_payload)
    assert not split_errors, split_errors
    assert split_plan is not None

    sha = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    decision = {"decision": "SPLIT_REQUIRED", "evidence_level": "HIGH"}
    envelope = build_envelope(
        candidate_name="candidate-broad-tool-bundle.md",
        candidate_path="CHAT_RESEARCH/candidate-broad-tool-bundle.md",
        candidate_id=sha[:20],
        candidate_sha=sha,
        decision=decision,
        split_plan=split_plan,
    )
    validated, errors = validate_envelope(envelope)
    assert not errors, errors
    assert validated is not None
    assert CONTROLLED_SPLIT_VERSION == "0.1.0"
    assert validated["split_plan"]["child_count"] == 2
    assert validated["candidate_write_performed"] is False
    assert validated["parent_transition_performed"] is False
    assert validated["canonical_write_performed"] is False
    assert validated["paid_model_used"] is False

    tampered = dict(envelope)
    tampered_plan = dict(split_plan)
    tampered_children = [dict(item) for item in split_plan["children"]]
    tampered_children[0]["markdown"] += "\nTAMPERED\n"
    tampered_plan["children"] = tampered_children
    tampered["split_plan"] = tampered_plan
    invalid, invalid_errors = validate_envelope(tampered)
    assert invalid is None
    assert "child_0_hash_mismatch" in invalid_errors

    wrong_parent = dict(envelope)
    wrong_plan = dict(split_plan)
    wrong_plan["parent_candidate_sha256"] = "0" * 64
    wrong_parent["split_plan"] = wrong_plan
    invalid_parent, parent_errors = validate_envelope(wrong_parent)
    assert invalid_parent is None
    assert "split_parent_sha_mismatch" in parent_errors

    assert plan_path_for("candidate-example.md", Path("output")) == Path("output/candidate-example.split-plan.json")

    print("candidate_split_controlled_smoke_ok envelope=1 hash_guard=1 parent_guard=1 canonical_write=0 paid_model=0")


if __name__ == "__main__":
    main()
