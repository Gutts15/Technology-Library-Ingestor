#!/usr/bin/env python3
"""Smoke tests for machine-readable candidate batch editorial acceptance."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_batch_editorial_acceptance import build_acceptance, validate_acceptance


def sample_report() -> dict:
    return {
        "state": "READY_FOR_EDITORIAL_REVIEW",
        "batch_id": "dba704d20a5ed3f6915c",
        "master_index_sha256": "a" * 64,
        "live_preconditions_verified": True,
        "canonical_write_performed": False,
        "counts": {"items": 2, "create": 2, "update": 0, "unchanged": 0},
        "items": [
            {
                "title": "B",
                "record_id": "b" * 20,
                "action": "CREATE",
                "target_path": "00_LIBRARY/B.md",
                "content_sha256": "b" * 64,
            },
            {
                "title": "A",
                "record_id": "a" * 20,
                "action": "CREATE",
                "target_path": "00_LIBRARY/A.md",
                "content_sha256": "c" * 64,
            },
        ],
    }


def main() -> None:
    report = sample_report()
    acceptance, errors = build_acceptance(report)
    assert not errors and acceptance is not None, errors
    assert acceptance["state"] == "EDITORIALLY_ACCEPTED"
    assert acceptance["editorial_review_complete"] is True
    assert acceptance["all_current_items_accepted"] is True
    assert acceptance["items"][0]["title"] == "A"
    assert acceptance["production_publish_authorized"] is False
    assert acceptance["canonical_write_performed"] is False
    assert validate_acceptance(acceptance, report) == []

    drifted = sample_report()
    drifted["items"][0]["content_sha256"] = "d" * 64
    assert "acceptance_item_bindings" in validate_acceptance(acceptance, drifted)

    bad = dict(acceptance)
    bad["production_publish_authorized"] = True
    assert "acceptance_production_publish_authorized" in validate_acceptance(bad, report)

    print(
        "candidate_batch_editorial_acceptance_smoke_ok exact_batch_binding=1 "
        "item_sha_binding=1 drift_blocked=1 authorization_separate=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
