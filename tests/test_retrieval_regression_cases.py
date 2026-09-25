#!/usr/bin/env python3
"""Validate the static Technology Library retrieval-regression case contract."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tests" / "retrieval_regression_cases.json"
ALLOWED_PHASES = {"CURRENT", "POST_CANDIDATE_BATCH"}


def main() -> None:
    payload = json.loads(CASES.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["suite_version"] == "public-synthetic-1"
    assert payload["current_library_record_count"] == 0
    cases = payload["cases"]
    assert isinstance(cases, list) and cases

    seen: set[str] = set()
    current = 0
    post = 0
    for case in cases:
        assert set(case).issuperset(
            {"id", "phase", "prompt", "expected_titles", "forbidden_titles", "max_canonical_records", "source_required"}
        )
        assert case["id"] not in seen
        seen.add(case["id"])
        assert case["phase"] in ALLOWED_PHASES
        assert isinstance(case["prompt"], str) and case["prompt"].startswith("TECH")
        assert isinstance(case["expected_titles"], list)
        assert isinstance(case["forbidden_titles"], list)
        assert 1 <= case["max_canonical_records"] <= 3
        assert isinstance(case["source_required"], bool)
        assert not set(case["expected_titles"]) & set(case["forbidden_titles"])
        if case["phase"] == "CURRENT":
            current += 1
            assert "optional_titles" not in case
            assert "conditional_title" not in case
        else:
            post += 1

    assert current >= 1
    assert post >= 1
    print(
        "retrieval_regression_cases_ok current=%d post=%d max_records=3 canonical_write=0"
        % (current, post)
    )


if __name__ == "__main__":
    main()
