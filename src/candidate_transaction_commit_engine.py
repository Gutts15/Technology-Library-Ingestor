#!/usr/bin/env python3
"""Build a terminal COMMITTED journal from verified canonical state.

This module performs no remote or canonical writes. It validates the durable journal
identity, a verified transaction receipt, and the receipt bytes that are about to be
persisted. Only then does it produce the exact terminal journal payload that permits
coordinator release.

The production caller must persist+readback the receipt, persist+readback the returned
COMMITTED journal, and only then call coordinator release_committed().
"""

from __future__ import annotations

import hashlib
from typing import Any

COMMIT_ENGINE_VERSION = "0.1.0"


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def build_committed_journal(
    journal: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
    receipt_raw: bytes,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(journal, dict):
        return None, ["journal_invalid"]
    if not isinstance(receipt, dict):
        return None, ["receipt_invalid"]

    errors: list[str] = []
    if journal.get("state") not in {"VERIFYING", "COMMITTED_PENDING_RECEIPT"}:
        errors.append("journal_not_commit_ready")
    if journal.get("canonical_write_performed") is not True:
        errors.append("journal_write_boundary_not_crossed")
    if journal.get("snapshots_verified") is not True:
        errors.append("journal_snapshots_not_verified")
    if journal.get("live_preconditions_verified_under_ownership") is not True:
        errors.append("journal_live_preconditions_not_verified")

    tx = journal.get("transaction_id")
    if not isinstance(tx, str) or not tx:
        errors.append("journal_transaction_id")
    if not isinstance(journal.get("batch_id"), str) or not journal.get("batch_id"):
        errors.append("journal_batch_id")
    if not isinstance(journal.get("owner"), str) or not journal.get("owner"):
        errors.append("journal_owner")
    if not isinstance(journal.get("journal_path"), str) or not journal.get("journal_path"):
        errors.append("journal_path")

    if receipt.get("state") != "VERIFIED_COMMITTED_STATE":
        errors.append("receipt_state")
    if receipt.get("candidate_settlement_eligible") is not True:
        errors.append("receipt_not_settlement_eligible")
    if receipt.get("transaction_id") != tx:
        errors.append("receipt_transaction_mismatch")
    if receipt.get("verification_only") is not True:
        errors.append("receipt_verification_flag")

    if not receipt_raw:
        errors.append("receipt_bytes_missing")

    if errors:
        return None, sorted(set(errors))

    terminal = dict(journal)
    terminal.update(
        {
            "commit_engine_version": COMMIT_ENGINE_VERSION,
            "state": "COMMITTED",
            "receipt_sha256": sha256(receipt_raw),
            "receipt_state": receipt.get("state"),
            "candidate_settlement_eligible": True,
            "coordinator_release_allowed": True,
            "safe_abort_verified": False,
            "canonical_write_performed": True,
        }
    )
    return terminal, []
