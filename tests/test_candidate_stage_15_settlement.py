#!/usr/bin/env python3
"""Smoke contracts for Stage 15 private settlement."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_stage_15_settlement import (
    STAGE15_VERSION,
    _candidate_private,
    transaction_ready_paths,
    validate_transaction_ready_payload,
)


def main() -> None:
    assert STAGE15_VERSION == "0.2.0"
    assert _candidate_private("99_INBOX/CANDIDATES/READY_FOR_CURATION/candidate-x.md").startswith(
        "99_INBOX/CANDIDATES/"
    )
    source, destination = transaction_ready_paths("a" * 20)
    assert source == "99_INBOX/CANDIDATES/TRANSACTION_READY/plan.json"
    assert destination == (
        "99_INBOX/CANDIDATES/RESOLVED/PUBLISHED/"
        + "a" * 20
        + "/AUDIT/TRANSACTION_READY/plan.json"
    )
    assert not validate_transaction_ready_payload(
        {"transaction_id": "a" * 20, "finalize_batch_id": "b" * 20},
        transaction_id="a" * 20,
        batch_id="b" * 20,
    )
    assert "transaction_ready_batch_mismatch" in validate_transaction_ready_payload(
        {"transaction_id": "a" * 20, "finalize_batch_id": "c" * 20},
        transaction_id="a" * 20,
        batch_id="b" * 20,
    )
    try:
        _candidate_private("00_LIBRARY/MASTER_INDEX.md")
    except ValueError:
        pass
    else:
        raise AssertionError("canonical path was not blocked")
    print("candidate_stage_15_settlement_smoke_ok private_boundary=1 canonical_write=0")


if __name__ == "__main__":
    main()
