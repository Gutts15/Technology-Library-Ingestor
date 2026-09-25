#!/usr/bin/env python3
"""Smoke tests for deterministic held-candidate diagnosis."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_hold_inspect import classify_candidate


def review(claims: list[str], state: str = "READY") -> dict:
    return {
        "schema_version": 1,
        "state": state,
        "counts": {"total": len(claims), "supported": len(claims), "partial": 0, "unsupported": 0, "conflict": 0},
        "publication_claims": [
            {"claim": claim, "support": "SUPPORTED", "source_indices": [1]}
            for claim in claims
        ],
    }


def curation(candidate_id: str, decision: str = "VALIDATED_NEW") -> dict:
    return {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "decision": decision,
        "package_id": "a" * 20,
        "revision_key": "b" * 20,
        "proposed_record": {
            "record_type": "TECHNOLOGY",
            "title": "Example Tool",
        },
    }


def main() -> None:
    cid = "c" * 20

    missing = classify_candidate(
        candidate_id=cid,
        claim_review=None,
        curation=curation(cid),
        selected_new_keys=set(),
        selected_update_ids=set(),
    )
    assert missing["reason"] == "CLAIM_REVIEW_MISSING"

    needs_review = classify_candidate(
        candidate_id=cid,
        claim_review=review([], state="NEEDS_REVIEW"),
        curation=curation(cid),
        selected_new_keys=set(),
        selected_update_ids=set(),
    )
    assert needs_review["reason"] == "CLAIM_REVIEW_NEEDS_REVIEW"

    metadata_only = classify_candidate(
        candidate_id=cid,
        claim_review=review([
            "The repository example/tool exists on GitHub.",
            "The repository has 12 stars.",
        ]),
        curation=curation(cid),
        selected_new_keys=set(),
        selected_update_ids=set(),
    )
    assert metadata_only["reason"] == "EDITORIAL_HOLD"
    assert metadata_only["editorial_state"] == "HOLD"
    assert "insufficient_functional_evidence" in metadata_only["editorial_reasons"]

    selected_new = classify_candidate(
        candidate_id=cid,
        claim_review=review(["The tool allows agents to control the editor."]),
        curation=curation(cid),
        selected_new_keys={("a" * 20, "b" * 20)},
        selected_update_ids=set(),
    )
    assert selected_new["state"] == "SELECTED"
    assert selected_new["reason"] == "CURRENT_BATCH_NEW"

    update_curation = curation(cid, decision="VALIDATED_UPDATE")
    selected_update = classify_candidate(
        candidate_id=cid,
        claim_review=review(["The tool is compatible with Editor 4."]),
        curation=update_curation,
        selected_new_keys=set(),
        selected_update_ids={cid},
    )
    assert selected_update["state"] == "SELECTED"
    assert selected_update["reason"] == "CURRENT_BATCH_UPDATE"

    downstream = classify_candidate(
        candidate_id=cid,
        claim_review=review(["The tool allows agents to control the editor."]),
        curation=curation(cid),
        selected_new_keys=set(),
        selected_update_ids=set(),
    )
    assert downstream["editorial_state"] == "READY"
    assert downstream["reason"] == "DOWNSTREAM_NEW_PREPARATION_MISSING_OR_FAILED"

    print(
        "candidate_hold_inspect_smoke_ok claim_hold=1 editorial_hold=1 selected_new=1 "
        "selected_update=1 downstream_diagnosis=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
