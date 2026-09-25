#!/usr/bin/env python3
"""Dependency-free smoke test for component-cohesion semantic guidance."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_semantic_local import (
    ADAPTER_VERSION,
    SCOPE_AUDIT_VERSION,
    build_prompt,
    build_scope_audit_prompt,
)


def main() -> None:
    candidate = """TYPE: CANDIDATE
CANDIDATE_STATUS: TO_REVIEW
PROPOSED_TYPE: TECHNOLOGY
PROPOSED_STATUS: TEST
PROPOSED_DOMAIN: 04_AI_AGENTS
PROPOSED_CATEGORY: INTEGRATIONS
TITLE: Example Godot Integration
LAST_CHECKED: 2026-09-11
SOURCE_ORIGIN: Synthetic test.

# Example Godot Integration

One editor addon works together with its companion MCP server.
"""
    probe = {
        "schema_version": 1,
        "sources": [
            {
                "status": "OK",
                "source_url": "https://example.com/integration",
                "excerpt": "The addon and MCP server are shipped together as one integration.",
            }
        ],
    }

    primary = build_prompt(candidate, probe, "# MASTER_INDEX\n")
    audit = build_scope_audit_prompt(candidate, probe)

    assert "Do not confuse architecture components with independent canonical subjects" in primary
    assert "companion MCP server/backend" in primary
    assert "same single source/repository" in primary
    assert "editor addon/plugin plus its companion MCP server/backend/bridge" in audit
    assert "realistic independent adoption" in audit
    assert "one Godot integration" in audit
    assert ADAPTER_VERSION == "0.5.0"
    assert SCOPE_AUDIT_VERSION == "0.2.0"

    print(
        "candidate_semantic_component_cohesion_prompt_smoke_ok "
        "bundled_components=1 same_source=1 independent_lifecycle=1 adapter=0.5.0 scope_audit=0.2.0"
    )


if __name__ == "__main__":
    main()
