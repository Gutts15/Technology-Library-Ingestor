#!/usr/bin/env python3
"""Dependency-free tests for strict split expectations."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_quality_expectations import EXPECTATION_GATE_VERSION, enforce_expectations


def make_report() -> dict:
    return {
        "items": [
            {
                "candidate_name": "candidate-a.md",
                "decision": "VALIDATED_NEW",
                "quality_pass": True,
                "quality_reasons": [],
            },
            {
                "candidate_name": "candidate-b.md",
                "decision": "SPLIT_REQUIRED",
                "quality_pass": True,
                "quality_reasons": [],
            },
        ],
        "passed": 2,
        "failed": 0,
        "overall_pass": True,
    }


def main() -> None:
    strict, errors = enforce_expectations(make_report(), set())
    assert not errors, errors
    assert strict is not None
    assert EXPECTATION_GATE_VERSION == "0.1.0"
    assert strict["passed"] == 1
    assert strict["failed"] == 1
    assert strict["overall_pass"] is False
    assert strict["unexpected_split"] == 1
    assert "unexpected_split_detected" in strict["items"][1]["quality_reasons"]
    assert strict["items"][1]["quality_pass"] is False

    expected, expected_errors = enforce_expectations(make_report(), {"candidate-b.md"})
    assert not expected_errors, expected_errors
    assert expected is not None
    assert expected["passed"] == 2
    assert expected["failed"] == 0
    assert expected["overall_pass"] is True
    assert expected["unexpected_split"] == 0

    missed_report = make_report()
    missed_report["items"][1]["decision"] = "VALIDATED_NEW"
    missed, missed_errors = enforce_expectations(missed_report, {"candidate-b.md"})
    assert not missed_errors, missed_errors
    assert missed is not None
    assert missed["overall_pass"] is False
    assert missed["expected_split_missed"] == 1
    assert "expected_split_not_detected" in missed["items"][1]["quality_reasons"]

    print("candidate_quality_expectations_smoke_ok unexpected_split=1 expected_split=1 missed_split=1 canonical_write=0")


if __name__ == "__main__":
    main()
