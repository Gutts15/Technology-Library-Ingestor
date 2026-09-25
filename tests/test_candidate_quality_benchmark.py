#!/usr/bin/env python3
"""Dependency-free tests for the local semantic quality benchmark."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_quality_benchmark import quality_checks, resolve_selected
from candidate_semantic_batch import normalize_candidate_text


def main() -> None:
    crlf = "TYPE: CANDIDATE\r\nTITLE: Example\r\n\r\nBody\r\n"
    lf = "TYPE: CANDIDATE\nTITLE: Example\n\nBody\n"
    assert normalize_candidate_text(crlf) == lf
    assert normalize_candidate_text(lf) == lf
    assert hashlib.sha256(normalize_candidate_text(crlf).encode("utf-8")).hexdigest() == hashlib.sha256(lf.encode("utf-8")).hexdigest()

    candidate_id = "a" * 20
    index = {
        "schema_version": 1,
        "entries": [
            {
                "candidate_id": candidate_id,
                "sha256": "b" * 64,
                "valid": True,
                "path": "CHAT_RESEARCH/candidate-broad-tooling.md",
            }
        ],
    }
    proposals = {
        "schema_version": 1,
        "entries": [{"candidate_id": candidate_id, "proposal": "READY_FOR_SEMANTIC"}],
    }
    selected, errors = resolve_selected(index, proposals, ["candidate-broad-tooling.md"])
    assert not errors, errors
    assert len(selected) == 1
    assert selected[0]["candidate_id"] == candidate_id

    missing, missing_errors = resolve_selected(index, proposals, ["candidate-missing.md"])
    assert not missing
    assert missing_errors == ["candidate_not_indexed:candidate-missing.md"]

    rich = {
        "final_review": {"decision": "SPLIT_REQUIRED"},
        "canonical_write_performed": False,
        "policy_reasons": ["multi_subject_candidate_requires_split"],
    }
    decision = {"decision": "SPLIT_REQUIRED", "evidence_level": "HIGH"}
    split_plan = {
        "child_count": 2,
        "candidate_write_performed": False,
        "canonical_write_performed": False,
        "paid_model_used": False,
    }
    passed, reasons = quality_checks(
        candidate_name="candidate-broad-tooling.md",
        decision=decision,
        rich_review=rich,
        expect_split=True,
        split_plan=split_plan,
        split_errors=[],
    )
    assert passed is True and not reasons, reasons

    missed_split, missed_reasons = quality_checks(
        candidate_name="candidate-broad-tooling.md",
        decision={"decision": "VALIDATED_NEW", "evidence_level": "HIGH"},
        rich_review={"final_review": {"decision": "VALIDATED_NEW"}, "canonical_write_performed": False},
        expect_split=True,
        split_plan=None,
        split_errors=[],
    )
    assert missed_split is False
    assert "expected_split_not_detected" in missed_reasons

    fail_closed, fail_reasons = quality_checks(
        candidate_name="candidate-broad-tooling.md",
        decision={"decision": "NEEDS_REVIEW", "evidence_level": "LOW"},
        rich_review={"final_review": {"decision": "NEEDS_REVIEW"}, "canonical_write_performed": False},
        expect_split=False,
        split_plan=None,
        split_errors=[],
    )
    assert fail_closed is False
    assert "semantic_fail_closed" in fail_reasons

    unsafe, unsafe_reasons = quality_checks(
        candidate_name="candidate-broad-tooling.md",
        decision=decision,
        rich_review={"final_review": {"decision": "SPLIT_REQUIRED"}, "canonical_write_performed": True},
        expect_split=True,
        split_plan={**split_plan, "canonical_write_performed": True},
        split_errors=[],
    )
    assert unsafe is False
    assert "unsafe_canonical_write_flag" in unsafe_reasons
    assert "unsafe_split_canonical_write_flag" in unsafe_reasons

    print("candidate_quality_benchmark_smoke_ok selection=1 split_expectation=1 fail_closed_guard=1 canonical_write_guard=1 newline_normalization=1")


if __name__ == "__main__":
    main()
