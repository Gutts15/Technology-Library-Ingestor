#!/usr/bin/env python3
"""Apply explicit split expectations to a local candidate quality report.

The semantic benchmark validates decision safety and split-plan integrity, but a
valid SPLIT_REQUIRED decision is not automatically a successful result when the
caller expected a candidate to be canonical-granular. This post-gate makes that
expectation explicit: only candidates listed with --expect-split may resolve to
SPLIT_REQUIRED. The report is updated in place and no private/canonical storage
is modified.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
from typing import Any

EXPECTATION_GATE_VERSION = "0.1.0"


def basename(value: str) -> str:
    return PurePosixPath(value).name


def enforce_expectations(
    report: dict[str, Any] | None,
    expected_split: set[str],
) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(report, dict):
        return None, ["report_invalid"]
    items = report.get("items")
    if not isinstance(items, list):
        return None, ["report_items_invalid"]

    selected_names: set[str] = set()
    unexpected_split = 0
    expected_split_missed = 0

    for raw in items:
        if not isinstance(raw, dict):
            return None, ["report_item_invalid"]
        name = raw.get("candidate_name")
        if not isinstance(name, str) or not name:
            return None, ["report_candidate_name_invalid"]
        name = basename(name)
        selected_names.add(name)

        existing_reasons = raw.get("quality_reasons")
        if existing_reasons is None:
            reasons: list[str] = []
        elif isinstance(existing_reasons, list) and all(isinstance(item, str) for item in existing_reasons):
            reasons = list(existing_reasons)
        else:
            return None, [f"quality_reasons_invalid:{name}"]

        actual = raw.get("decision")
        if actual == "SPLIT_REQUIRED" and name not in expected_split:
            if "unexpected_split_detected" not in reasons:
                reasons.append("unexpected_split_detected")
            unexpected_split += 1
        if name in expected_split and actual != "SPLIT_REQUIRED":
            if "expected_split_not_detected" not in reasons:
                reasons.append("expected_split_not_detected")
            expected_split_missed += 1

        raw["quality_reasons"] = sorted(set(reasons))
        raw["quality_pass"] = not raw["quality_reasons"]

    unknown_expected = sorted(expected_split - selected_names)
    if unknown_expected:
        return None, [f"expected_split_not_selected:{name}" for name in unknown_expected]

    passed = sum(1 for item in items if isinstance(item, dict) and item.get("quality_pass") is True)
    report["expectation_gate_version"] = EXPECTATION_GATE_VERSION
    report["passed"] = passed
    report["failed"] = len(items) - passed
    report["overall_pass"] = passed == len(items)
    report["unexpected_split"] = unexpected_split
    report["expected_split_missed"] = expected_split_missed
    return report, []


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply strict split expectations to a candidate benchmark report.")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--expect-split", action="append", default=[])
    args = parser.parse_args()

    try:
        payload = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        print("candidate_quality_expectations_error code=report_read_failed")
        return 2

    expected = {basename(value) for value in args.expect_split}
    updated, errors = enforce_expectations(payload, expected)
    if errors or updated is None:
        print(f"candidate_quality_expectations_error codes={','.join(errors or ['expectation_failed'])}")
        return 2

    args.report.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_quality_expectations_ok "
        f"selected={len(updated['items'])} passed={updated['passed']} failed={updated['failed']} "
        f"unexpected_split={updated['unexpected_split']} expected_split_missed={updated['expected_split_missed']} "
        f"overall_pass={int(updated['overall_pass'])} canonical_write=0 paid_model=0"
    )
    return 0 if updated["overall_pass"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
