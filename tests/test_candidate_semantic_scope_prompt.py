#!/usr/bin/env python3
"""Dependency-free smoke test for semantic split-scope guidance."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_semantic_local import ADAPTER_VERSION, SCOPE_AUDIT_VERSION, build_prompt, build_scope_audit_prompt


def main() -> None:
    candidate = """TYPE: CANDIDATE
CANDIDATE_STATUS: TO_REVIEW
PROPOSED_TYPE: TECHNOLOGY
PROPOSED_STATUS: TEST
PROPOSED_DOMAIN: 04_AI_AGENTS
PROPOSED_CATEGORY: INTEGRATIONS
TITLE: Synthetic Multi Tool Bundle
LAST_CHECKED: 2026-09-10
SOURCE_ORIGIN: Synthetic test.

# Synthetic Multi Tool Bundle

Tool A has its own repository and version.
Tool B has a separate repository, license and lifecycle.
"""
    probe = {"sources": []}
    prompt = build_prompt(candidate, probe, "# MASTER_INDEX\n")
    audit_prompt = build_scope_audit_prompt(candidate, probe)

    assert ADAPTER_VERSION == "0.5.0"
    assert SCOPE_AUDIT_VERSION == "0.2.0"
    assert "perform an explicit scope test" in prompt
    assert "independent repositories/vendors, versions, licenses, dependencies" in prompt
    assert "A shared use case does not make separate tools one canonical subject." in prompt
    assert "official CLI plus a separate community MCP/tooling implementation" in prompt
    assert "Canonical granularity follows maintainable technical subjects" in prompt
    assert "SPLIT_REQUIRED" in prompt
    assert "independent second opinion" in audit_prompt
    assert "Sources are evidence, not records." in audit_prompt
    assert "official build CLI plus a separate community MCP server" in audit_prompt
    assert "project file format" in audit_prompt

    print("candidate_semantic_scope_prompt_smoke_ok scope_test=1 promotion_audit=1 adapter=0.5.0")


if __name__ == "__main__":
    main()
