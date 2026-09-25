#!/usr/bin/env python3
"""Dependency-free smoke tests for second-pass promotion scope auditing."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_semantic_local import apply_scope_audit, build_outputs


def fixture() -> tuple[str, dict, dict]:
    candidate = """TYPE: CANDIDATE
CANDIDATE_STATUS: TO_REVIEW
PROPOSED_TYPE: TECHNOLOGY
PROPOSED_STATUS: TEST
PROPOSED_DOMAIN: 04_AI_AGENTS
PROPOSED_CATEGORY: INTEGRATIONS
TITLE: Synthetic Automation Bundle
LAST_CHECKED: 2026-09-11
SOURCE_ORIGIN: Synthetic test.

# Synthetic Automation Bundle

Official Build CLI automates builds. Project file format explains resources. Separate Community MCP Server manipulates projects.
"""
    probe = {
        "sources": [
            {"status": "OK", "source_url": "https://example.com/cli", "excerpt": "Official Build CLI."},
            {"status": "OK", "source_url": "https://example.com/format", "excerpt": "Project file format."},
            {"status": "OK", "source_url": "https://example.com/mcp", "excerpt": "Community MCP Server."},
        ]
    }
    primary = {
        "decision": "VALIDATED_NEW",
        "evidence_level": "HIGH",
        "proposed_type": "TECHNOLOGY",
        "proposed_status": "TEST",
        "canonical_match_path": None,
        "rationale": "The bundle is supported by evidence.",
    }
    return candidate, probe, primary


def main() -> None:
    candidate, probe, primary = fixture()
    rich, decision, errors = build_outputs(candidate, probe, primary)
    assert not errors, errors
    assert rich is not None and decision is not None
    assert rich["final_review"]["decision"] == "VALIDATED_NEW"

    split_audit = {
        "requires_split": True,
        "subjects": ["Official Build CLI", "Community MCP Server"],
        "rationale": "The CLI and MCP server have independent technical identities; the project format is support-only.",
    }
    split_rich, split_decision = apply_scope_audit(rich, decision, split_audit)
    assert split_rich["final_review"]["decision"] == "SPLIT_REQUIRED"
    assert split_rich["final_review"]["proposed_type"] is None
    assert split_rich["final_review"]["proposed_status"] is None
    assert split_rich["final_review"]["canonical_match_path"] is None
    assert "promotion_scope_audit_requires_split" in split_rich["policy_reasons"]
    assert split_decision["items"][0]["decision"] == "SPLIT_REQUIRED"
    assert split_rich["scope_audit"]["subjects"] == ["Official Build CLI", "Community MCP Server"]

    candidate, probe, primary = fixture()
    rich, decision, errors = build_outputs(candidate, probe, primary)
    assert not errors and rich is not None and decision is not None
    no_split_audit = {
        "requires_split": False,
        "subjects": ["Synthetic Automation Bundle"],
        "rationale": "The material supports one independently maintainable subject.",
    }
    kept_rich, kept_decision = apply_scope_audit(rich, decision, no_split_audit)
    assert kept_rich["final_review"]["decision"] == "VALIDATED_NEW"
    assert kept_decision["items"][0]["decision"] == "VALIDATED_NEW"

    candidate, probe, primary = fixture()
    rich, decision, errors = build_outputs(candidate, probe, primary)
    assert not errors and rich is not None and decision is not None
    failed_rich, failed_decision = apply_scope_audit(rich, decision, None)
    assert failed_rich["final_review"]["decision"] == "NEEDS_REVIEW"
    assert failed_rich["final_review"]["evidence_level"] == "LOW"
    assert "promotion_scope_audit_failed" in failed_rich["policy_reasons"]
    assert failed_decision["items"][0]["decision"] == "NEEDS_REVIEW"

    sha = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    assert failed_rich["candidate_id"] == sha[:20]

    print("candidate_semantic_scope_audit_smoke_ok split_override=1 keep_promotion=1 fail_closed=1 canonical_write=0")


if __name__ == "__main__":
    main()
