#!/usr/bin/env python3
"""Smoke test for terminal committed-journal construction."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_transaction_commit_engine import build_committed_journal, sha256


def main() -> None:
    tx = "a" * 20
    journal = {
        "state": "COMMITTED_PENDING_RECEIPT",
        "transaction_id": tx,
        "batch_id": "b" * 20,
        "owner": "candidate-publisher-" + "c" * 12,
        "journal_path": "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/x/journal.json",
        "canonical_write_performed": True,
        "snapshots_verified": True,
        "live_preconditions_verified_under_ownership": True,
        "coordinator_release_allowed": False,
    }
    receipt = {
        "state": "VERIFIED_COMMITTED_STATE",
        "transaction_id": tx,
        "candidate_settlement_eligible": True,
        "verification_only": True,
    }
    raw = (json.dumps(receipt, sort_keys=True) + "\n").encode("utf-8")

    terminal, errors = build_committed_journal(journal, receipt, raw)
    assert not errors, errors
    assert terminal is not None
    assert terminal["state"] == "COMMITTED"
    assert terminal["receipt_sha256"] == sha256(raw)
    assert terminal["coordinator_release_allowed"] is True
    assert terminal["canonical_write_performed"] is True

    bad = dict(journal)
    bad["canonical_write_performed"] = False
    terminal, errors = build_committed_journal(bad, receipt, raw)
    assert terminal is None
    assert "journal_write_boundary_not_crossed" in errors

    print(
        "candidate_transaction_commit_engine_smoke_ok receipt_bound=1 "
        "terminal_commit=1 release_gate=1 write_boundary_required=1"
    )


if __name__ == "__main__":
    main()
