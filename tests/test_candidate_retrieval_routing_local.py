#!/usr/bin/env python3
"""Smoke tests for fresh-conversation virtual candidate retrieval routing."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_retrieval_routing_local import evaluate_case, parse_selection


def main() -> None:
    available = {
        "Synthetic Engine Command-Line Interface",
        "Synthetic Engine MCP Server",
        "Synthetic Editor Bridge",
        "Synthetic Editor Plugin",
        "example-org/synthetic-mcp",
        "Synthetic UI Kit",
    }

    case = {
        "id": "gm",
        "phase": "POST_CANDIDATE_BATCH",
        "expected_titles": ["Synthetic Engine Command-Line Interface"],
        "optional_titles": ["Synthetic Engine MCP Server"],
        "forbidden_titles": ["Synthetic Editor Bridge"],
        "max_canonical_records": 3,
        "source_required": False,
    }
    selection, errors = parse_selection(
        {
            "selected_titles": ["Synthetic Engine Command-Line Interface"],
            "source_lookup_required": False,
            "rationale": "CLI matches the automation request.",
        }
    )
    assert not errors and selection is not None
    result = evaluate_case(case, selection, available)
    assert result["state"] == "PASS", result

    bad = dict(selection)
    bad["selected_titles"] = ["Synthetic Editor Bridge"]
    result = evaluate_case(case, bad, available)
    assert result["state"] == "FAIL"
    assert "missing_expected" in result["reasons"]
    assert "selected_forbidden" in result["reasons"]

    provenance = dict(case)
    provenance["id"] = "source"
    provenance["expected_titles"] = ["Synthetic UI Kit"]
    provenance["optional_titles"] = []
    provenance["forbidden_titles"] = []
    provenance["source_required"] = True
    source_selection = {
        "selected_titles": ["Synthetic UI Kit"],
        "source_lookup_required": False,
        "rationale": "Wrongly skipped provenance lookup.",
    }
    result = evaluate_case(provenance, source_selection, available)
    assert result["state"] == "FAIL"
    assert "source_lookup_mismatch" in result["reasons"]

    conditional = dict(case)
    conditional["id"] = "conditional"
    conditional["expected_titles"] = []
    conditional["optional_titles"] = []
    conditional["forbidden_titles"] = []
    conditional["conditional_title"] = "Synthetic Engine MCP Server"
    conditional_selection = {
        "selected_titles": ["Synthetic Engine MCP Server"],
        "source_lookup_required": False,
        "rationale": "The conditional record is present in the virtual catalog.",
    }
    result = evaluate_case(conditional, conditional_selection, available)
    assert result["state"] == "PASS", result
    assert result["expected_titles"] == ["Synthetic Engine MCP Server"]

    unknown = dict(selection)
    unknown["selected_titles"] = ["Invented Technology"]
    result = evaluate_case(case, unknown, available)
    assert result["state"] == "FAIL"
    assert "unknown_title" in result["reasons"]

    parsed, parse_errors = parse_selection(
        {
            "selected_titles": ["Synthetic UI Kit", "Synthetic UI Kit"],
            "source_lookup_required": False,
            "rationale": "duplicate",
        }
    )
    assert parsed is None
    assert "duplicate_selected_title" in parse_errors

    print(
        "candidate_retrieval_routing_local_smoke_ok exact_expected=1 optional_allowed=1 "
        "forbidden_blocked=1 provenance_enforced=1 conditional_supported=1 unknown_blocked=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
