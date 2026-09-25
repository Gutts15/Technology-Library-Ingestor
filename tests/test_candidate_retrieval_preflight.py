#!/usr/bin/env python3
"""Smoke tests for the virtual post-batch retrieval catalog preflight."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_retrieval_preflight import build_preflight_report


EXISTING_PATH = "00_LIBRARY/01_GAME_DEVELOPMENT/CODE/pattern-existing-pattern.md"
NEW_TARGET = "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-new-bridge.md"
NEW_PRIVATE = "99_INBOX/CANDIDATES/PUBLISH_READY/records/bbbbbbbbbbbbbbbbbbbb.md"


def existing_record() -> bytes:
    return """RECORD_ID: aaaaaaaaaaaaaaaaaaaa
TYPE: PATTERN
STATUS: REFERENCE
DOMAIN: 01_GAME_DEVELOPMENT
CATEGORY: CODE
TITLE: Existing Pattern

# Existing Pattern

## PURPOSE

Reusable behavior.
""".encode("utf-8")


def new_record(title: str = "New Bridge") -> bytes:
    return f"""RECORD_ID: bbbbbbbbbbbbbbbbbbbb
TYPE: TECHNOLOGY
STATUS: TEST
DOMAIN: 04_AI_AGENTS
CATEGORY: INTEGRATIONS
TITLE: {title}

# {title}

## SUMMARY

Controls an editor through a bridge.
""".encode("utf-8")


def cases() -> dict:
    return {
        "schema_version": 1,
        "suite_version": "0.1.0",
        "cases": [
            {
                "id": "current-existing",
                "phase": "CURRENT",
                "prompt": "TECH: existing?",
                "expected_titles": ["Existing Pattern"],
                "forbidden_titles": [],
                "max_canonical_records": 3,
                "source_required": False,
            },
            {
                "id": "post-new",
                "phase": "POST_CANDIDATE_BATCH",
                "prompt": "TECH: new?",
                "expected_titles": ["New Bridge"],
                "optional_titles": [],
                "forbidden_titles": ["Existing Pattern"],
                "max_canonical_records": 3,
                "source_required": False,
            },
            {
                "id": "post-conditional",
                "phase": "POST_CANDIDATE_BATCH",
                "prompt": "TECH: conditional?",
                "expected_titles": [],
                "conditional_title": "Optional Community Tool",
                "condition": "Allowed to remain absent.",
                "forbidden_titles": [],
                "max_canonical_records": 3,
                "source_required": False,
            },
        ],
    }


def report() -> dict:
    return {
        "batch_id": "c" * 20,
        "items": [
            {
                "lane": "NEW",
                "action": "CREATE",
                "record_id": "b" * 20,
                "record_type": "TECHNOLOGY",
                "status": "TEST",
                "domain": "04_AI_AGENTS",
                "category": "INTEGRATIONS",
                "title": "New Bridge",
                "target_path": NEW_TARGET,
                "content_path": NEW_PRIVATE,
            }
        ],
    }


def main() -> None:
    content = {
        EXISTING_PATH: existing_record(),
        NEW_PRIVATE: new_record(),
    }
    result, errors = build_preflight_report(
        canonical_paths=[EXISTING_PATH],
        batch_report=report(),
        cases_payload=cases(),
        read_content=lambda path: content.get(path),
    )
    assert not errors, errors
    assert result is not None
    assert result["state"] == "PASS"
    assert result["virtual_record_count"] == 2
    assert result["fresh_conversation_retrieval_still_required"] is True
    assert result["routing_quality_proven"] is False
    assert result["canonical_write_performed"] is False
    states = {row["id"]: row for row in result["structural_cases"]}
    assert states["current-existing"]["state"] == "PASS"
    assert states["post-new"]["state"] == "PASS"
    assert states["post-conditional"]["conditional_state"] == "ABSENT_ALLOWED"
    assert states["post-new"]["forbidden_titles_not_evaluated_structurally"] == ["Existing Pattern"]

    missing_cases = cases()
    missing_cases["cases"][1]["expected_titles"] = ["Missing Technology"]
    failed, failed_errors = build_preflight_report(
        canonical_paths=[EXISTING_PATH],
        batch_report=report(),
        cases_payload=missing_cases,
        read_content=lambda path: content.get(path),
    )
    assert not failed_errors, failed_errors
    assert failed is not None and failed["state"] == "FAIL"
    assert failed["failed_case_ids"] == ["post-new"]

    # Virtual duplicate canonical titles must fail closed before any routing claim.
    duplicate_path = "00_LIBRARY/01_GAME_DEVELOPMENT/CODE/pattern-existing-pattern-copy.md"
    duplicate = """RECORD_ID: dddddddddddddddddddd
TYPE: PATTERN
STATUS: TEST
DOMAIN: 01_GAME_DEVELOPMENT
CATEGORY: CODE
TITLE: Existing Pattern

# Existing Pattern
""".encode("utf-8")
    duplicate_content = dict(content)
    duplicate_content[duplicate_path] = duplicate
    duplicate_result, duplicate_errors = build_preflight_report(
        canonical_paths=[EXISTING_PATH, duplicate_path],
        batch_report=report(),
        cases_payload=cases(),
        read_content=lambda path: duplicate_content.get(path),
    )
    assert duplicate_result is None
    assert "virtual_duplicate_title:PATTERN:Existing Pattern" in duplicate_errors

    print(
        "candidate_retrieval_preflight_smoke_ok virtual_overlay=1 index_build=1 regression_targets=1 "
        "conditional_absence=1 routing_claim_deferred=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
