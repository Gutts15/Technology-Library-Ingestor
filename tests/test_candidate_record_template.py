#!/usr/bin/env python3
"""Dependency-free smoke tests for type-aware candidate canonical body templates."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_record_template import TEMPLATE_VERSION, render_body, section_plan, summary_items


def item(claim: str, kind: str, ref: int = 1) -> dict:
    return {
        "claim": claim,
        "support": "SUPPORTED",
        "source_indices": [ref],
        "editorial_kind": kind,
    }


def editorial() -> dict:
    return {
        "publication_claims": [
            item("The integration controls an editor through MCP.", "CAPABILITY"),
            item("The workflow requires Node.js 22.", "COMPATIBILITY", 2),
            item("Install the package with npm install.", "INSTALLATION", 2),
            item("Destructive operations require confirmation.", "LIMITATION", 3),
            item("The project uses the MIT license.", "LICENSE", 4),
            item("The project is a community integration.", "IDENTITY", 5),
            item("Use ordered validation stages before publication.", "OTHER", 6),
        ]
    }


def headings(lines: list[str]) -> list[str]:
    return [line[3:] for line in lines if line.startswith("## ")]


def main() -> None:
    review = editorial()

    technology = render_body("TECHNOLOGY", "Tool", review)
    assert headings(technology) == [
        "SUMMARY",
        "LIMITATIONS AND SAFETY",
        "LICENSE",
        "OTHER VERIFIED TECHNICAL NOTES",
    ]
    assert technology.count("- The integration controls an editor through MCP. [S1]") == 1
    assert technology.count("- The workflow requires Node.js 22. [S2]") == 1
    assert technology.count("- Install the package with npm install. [S2]") == 1

    pattern = render_body("PATTERN", "Reusable Pattern", review)
    assert headings(pattern) == [
        "SUMMARY",
        "IMPLEMENTATION GUIDANCE",
        "COMPATIBILITY",
        "PROVENANCE NOTES",
        "LICENSE",
    ]
    assert "## VERIFIED CAPABILITIES" not in pattern
    assert "- Use ordered validation stages before publication. [S6]" in pattern
    assert pattern.count("- The integration controls an editor through MCP. [S1]") == 1

    pipeline = render_body("PIPELINE", "Safe Pipeline", review)
    assert headings(pipeline) == [
        "PURPOSE",
        "SETUP AND PREREQUISITES",
        "LIMITATIONS AND SAFETY",
        "PROVENANCE NOTES",
        "LICENSE",
    ]
    assert "## SUMMARY" not in pipeline
    assert pipeline.count("- Install the package with npm install. [S2]") == 1

    source = render_body("SOURCE", "Captured Evidence", review)
    assert headings(source) == [
        "SOURCE CONTENT",
        "VERIFICATION CONTEXT",
    ]
    assert "## VERIFIED CAPABILITIES" not in source
    assert "- The project is a community integration. [S5]" in source
    assert source.count("- The integration controls an editor through MCP. [S1]") == 1

    for record_type in ("TECHNOLOGY", "PATTERN", "PIPELINE", "SOURCE"):
        summary = summary_items(review, record_type)
        assert 1 <= len(summary) <= 3
        assert all(entry in review["publication_claims"] for entry in summary)
        assert section_plan(record_type, review)

        rendered = render_body(record_type, "Example", review)
        for entry in summary:
            rendered_line = f"- {entry['claim']} [S{entry['source_indices'][0]}]"
            assert rendered.count(rendered_line) == 1

    try:
        render_body("UNKNOWN", "Bad", review)
        raise AssertionError("unknown type should fail")
    except ValueError as exc:
        assert str(exc) == "record_type_invalid"

    print(
        "candidate_record_template_smoke_ok technology=1 pattern=1 pipeline=1 source=1 "
        "summary_dedup=1 "
        f"template_version={TEMPLATE_VERSION} canonical_write=0"
    )


if __name__ == "__main__":
    main()
