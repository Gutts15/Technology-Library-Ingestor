#!/usr/bin/env python3
"""Dependency-free tests for FILE_EVIDENCE semantic planning."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from file_evidence_semantic_plan import build_model_response_schema, build_plan, envelope_binding, validate_model_payload


def fixture() -> tuple[dict, str]:
    envelope = {
        "schema_version": 1,
        "bridge_version": "0.1.0",
        "type": "FILE_EVIDENCE_ENVELOPE",
        "state": "READY_FOR_SEMANTIC_EXTRACTION",
        "envelope_id": "a" * 20,
        "package_id": "b" * 20,
        "revision_key": "c" * 20,
        "kind": "document",
        "evidence_summary_sha256": "d" * 64,
        "pointers": {
            "evidence": "99_INBOX/READY_FOR_ANALYSIS/bbbbbbbbbbbbbbbbbbbb/evidence-summary.json",
            "detail": None,
            "preview": None,
        },
        "semantic_summary": {
            "schema_version": 1,
            "content": {"type": "pdf"},
            "evidence": {"samples": ["Tool Alpha provides editor automation."]},
        },
        "candidate_created": False,
        "curation_transition_performed": False,
        "canonical_write_performed": False,
        "paid_model_used": False,
    }
    master = """# MASTER_INDEX

## 04_AI_AGENTS
### INTEGRATIONS
- TECHNOLOGY - Existing Tool [REFERENCE] (`00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-existing-tool.md`)
- TECHNOLOGY - Tool Alpha [TEST] (`00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-tool-alpha.md`)
"""
    return envelope, master


def main() -> None:
    schema = build_model_response_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["candidates"]["maxItems"] == 8
    candidate_schema = schema["properties"]["candidates"]["items"]
    assert candidate_schema["additionalProperties"] is False
    assert "SOURCE" in candidate_schema["properties"]["proposed_type"]["enum"]
    assert "SOURCES" in candidate_schema["properties"]["proposed_domain"]["pattern"]

    automatic_schema = build_model_response_schema(
        max_candidates=1,
        allowed_types={"TECHNOLOGY", "PATTERN", "PIPELINE"},
    )
    automatic_candidate = automatic_schema["properties"]["candidates"]["items"]
    assert automatic_schema["properties"]["candidates"]["maxItems"] == 1
    assert set(automatic_candidate["properties"]["proposed_type"]["enum"]) == {
        "TECHNOLOGY", "PATTERN", "PIPELINE"
    }
    assert "SOURCES" not in automatic_candidate["properties"]["proposed_domain"]["pattern"]

    envelope, master = fixture()
    binding, binding_errors = envelope_binding(envelope)
    assert not binding_errors, binding_errors
    assert binding is not None and binding["package_id"] == "b" * 20

    payload = {
        "outcome": "CANDIDATES_PROPOSED",
        "rationale": "The document contains one reusable technical subject.",
        "candidates": [
            {
                "title": "Tool Alpha",
                "proposed_type": "TECHNOLOGY",
                "proposed_status": "TEST",
                "proposed_domain": "04_AI_AGENTS",
                "proposed_category": "INTEGRATIONS",
                "summary": "Tool Alpha provides editor automation.",
                "claims": ["Tool Alpha provides editor automation."],
            },
            {
                "title": "Tool Beta",
                "proposed_type": "TECHNOLOGY",
                "proposed_status": "TEST",
                "proposed_domain": "04_AI_AGENTS",
                "proposed_category": "INTEGRATIONS",
                "summary": "Tool Beta is a separate reusable integration.",
                "claims": ["Tool Beta is a separate reusable integration."],
            },
        ],
    }
    reviewed, review_errors = validate_model_payload(payload, master)
    assert not review_errors, review_errors
    assert reviewed is not None
    assert reviewed["candidates"][0]["disposition"] == "POTENTIAL_UPDATE"
    assert reviewed["candidates"][0]["canonical_matches"] == [
        "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-tool-alpha.md"
    ]
    assert reviewed["candidates"][1]["disposition"] == "NEW"

    plan, errors = build_plan(envelope, master, payload)
    assert not errors, errors
    assert plan is not None
    assert plan["outcome"] == "CANDIDATES_PROPOSED"
    assert len(plan["candidates"]) == 2
    assert plan["candidate_write_performed"] is False
    assert plan["curation_transition_performed"] is False
    assert plan["canonical_write_performed"] is False
    assert plan["paid_model_used"] is False

    no_knowledge = {
        "outcome": "NO_REUSABLE_KNOWLEDGE",
        "rationale": "The artifact is technical project evidence but contains no reusable general knowledge.",
        "candidates": [],
    }
    no_plan, no_errors = build_plan(envelope, master, no_knowledge)
    assert not no_errors, no_errors
    assert no_plan is not None and no_plan["candidates"] == []

    accidental = {
        "outcome": "SUSPECTED_ACCIDENTAL",
        "rationale": "The evidence appears unrelated to a technical knowledge library.",
        "candidates": [],
    }
    accidental_plan, accidental_errors = build_plan(envelope, master, accidental)
    assert not accidental_errors, accidental_errors
    assert accidental_plan is not None and accidental_plan["outcome"] == "SUSPECTED_ACCIDENTAL"

    unsafe_reference = {
        "outcome": "CANDIDATES_PROPOSED",
        "rationale": "Invalid automatic confidence escalation.",
        "candidates": [
            {
                **payload["candidates"][1],
                "proposed_status": "REFERENCE",
            }
        ],
    }
    rejected, rejected_errors = build_plan(envelope, master, unsafe_reference)
    assert rejected is None
    assert "candidate_0_status" in rejected_errors

    duplicate_titles = {
        "outcome": "CANDIDATES_PROPOSED",
        "rationale": "Duplicate output should fail.",
        "candidates": [payload["candidates"][1], payload["candidates"][1]],
    }
    duplicate, duplicate_errors = build_plan(envelope, master, duplicate_titles)
    assert duplicate is None
    assert "candidate_1_duplicate_title" in duplicate_errors

    bad_nonempty = {
        "outcome": "NEEDS_REVIEW",
        "rationale": "Ambiguous evidence.",
        "candidates": [payload["candidates"][1]],
    }
    invalid, invalid_errors = build_plan(envelope, master, bad_nonempty)
    assert invalid is None
    assert "candidate_list_forbidden" in invalid_errors

    changed = dict(envelope)
    changed["canonical_write_performed"] = True
    bad_binding, bad_binding_errors = envelope_binding(changed)
    assert bad_binding is None
    assert "unsafe_canonical_write_performed" in bad_binding_errors

    print("file_evidence_semantic_plan_smoke_ok proposed=2 candidate_write=0 canonical_write=0 paid_model=0")


if __name__ == "__main__":
    main()
