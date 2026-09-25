#!/usr/bin/env python3
"""Dependency-free smoke test for canonical child scope guidance."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_split_plan import SPLIT_PLAN_VERSION, build_prompt


def main() -> None:
    candidate = """TYPE: CANDIDATE
CANDIDATE_STATUS: TO_REVIEW
PROPOSED_TYPE: TECHNOLOGY
PROPOSED_STATUS: TEST
PROPOSED_DOMAIN: 04_AI_AGENTS
PROPOSED_CATEGORY: INTEGRATIONS
TITLE: Synthetic Game Automation Bundle
LAST_CHECKED: 2026-09-11
SOURCE_ORIGIN: Synthetic test.

# Synthetic Game Automation Bundle

Official Build CLI automates builds. The engine project format explains how resources are stored. A separate community MCP server manipulates projects.
"""
    review = {
        "final_review": {
            "decision": "SPLIT_REQUIRED",
            "rationale": "The CLI and MCP server are independently maintainable subjects; the file format may only support them.",
        }
    }
    probe = {
        "sources": [
            {"status": "OK", "source_url": "https://example.com/cli", "excerpt": "Official Build CLI."},
            {"status": "OK", "source_url": "https://example.com/format", "excerpt": "Project file format."},
            {"status": "OK", "source_url": "https://example.com/mcp", "excerpt": "Community MCP server."},
        ]
    }

    prompt = build_prompt(candidate, review, probe)

    assert SPLIT_PLAN_VERSION == "0.2.0"
    assert "Sources are evidence, not records." in prompt
    assert "supporting details such as project/file formats" in prompt
    assert "Keep support-only facts inside the summary/evidence" in prompt
    assert "Never copy the parent route mechanically." in prompt
    assert "omit a support-only child rather than forcing it" in prompt
    assert "the likely canonical children are the CLI and MCP server" in prompt
    assert "The file format normally remains supporting evidence" in prompt

    print("candidate_split_canonical_scope_prompt_smoke_ok canonical_subject=1 support_only=1 routing=1 split_plan=0.2.0")


if __name__ == "__main__":
    main()
