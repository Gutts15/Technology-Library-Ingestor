#!/usr/bin/env python3
"""Smoke tests for exclusive fixture transaction lease behavior."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_transaction_fixture import FIXTURE_MARKER, FIXTURE_MARKER_VALUE
from candidate_transaction_lease_fixture import acquire_lease, release_lease, release_recovered_lease
from candidate_transaction_recovery_fixture import RECOVERY_VERSION, journal_dir


def write_recovery_state(root: Path, transaction_id: str, state: str, verified: bool) -> None:
    path = journal_dir(root, transaction_id) / "journal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "recovery_version": RECOVERY_VERSION,
        "transaction_id": transaction_id,
        "state": state,
        "rollback_verified": verified,
        "receipt_verified": state == "COMMITTED" and verified,
        "fixture_only": True,
        "canonical_write_performed": False,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    tx1 = "a" * 20
    tx2 = "b" * 20

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / FIXTURE_MARKER).write_text(FIXTURE_MARKER_VALUE, encoding="utf-8")

        lease, errors = acquire_lease(root, tx1, "owner-a")
        assert not errors, errors
        assert lease is not None
        assert lease["transaction_id"] == tx1
        assert lease["owner"] == "owner-a"
        assert lease["canonical_write_performed"] is False

        # Exact same owner/transaction is idempotent.
        same, same_errors = acquire_lease(root, tx1, "owner-a")
        assert not same_errors, same_errors
        assert same == lease

        # Different owner cannot silently take over the same transaction.
        conflict, conflict_errors = acquire_lease(root, tx1, "owner-b")
        assert conflict is None
        assert conflict_errors == ["lease_conflict"]

        # Different transaction is also blocked while the lease is active.
        conflict, conflict_errors = acquire_lease(root, tx2, "owner-a")
        assert conflict is None
        assert conflict_errors == ["lease_conflict"]

        # Release requires the exact owner and transaction.
        assert release_lease(root, tx1, "owner-b") == ["lease_owner_mismatch"]
        assert release_lease(root, tx2, "owner-a") == ["lease_transaction_mismatch"]
        assert release_lease(root, tx1, "owner-a") == []

        # Once released, a different transaction can acquire the fixture lease.
        second, second_errors = acquire_lease(root, tx2, "owner-b")
        assert not second_errors, second_errors
        assert second is not None and second["transaction_id"] == tx2
        assert release_lease(root, tx2, "owner-b") == []

        # Simulate a stale lease left by a crashed owner. It cannot be cleared
        # merely because somebody wants to continue; exact recovery must finish.
        stale, stale_errors = acquire_lease(root, tx1, "dead-owner")
        assert not stale_errors and stale is not None
        assert release_recovered_lease(root, tx1) == ["recovery_journal_missing"]
        write_recovery_state(root, tx1, "PREPARED", False)
        assert release_lease(root, tx1, "dead-owner") == ["transaction_not_finalized"]
        assert release_recovered_lease(root, tx1) == ["recovery_not_complete"]
        write_recovery_state(root, tx1, "RECOVERED", False)
        assert release_lease(root, tx1, "dead-owner") == ["transaction_not_finalized"]
        assert release_recovered_lease(root, tx1) == ["recovery_not_verified"]
        write_recovery_state(root, tx1, "RECOVERED", True)
        assert release_recovered_lease(root, tx1) == []

        # Only after verified recovery may a different transaction proceed.
        after_recovery, after_errors = acquire_lease(root, tx2, "owner-after-recovery")
        assert not after_errors and after_recovery is not None
        assert release_lease(root, tx2, "owner-after-recovery") == []

        # A normal owner may release after a journal is durably committed.
        committed_lease, committed_errors = acquire_lease(root, tx1, "owner-committed")
        assert not committed_errors and committed_lease is not None
        write_recovery_state(root, tx1, "COMMITTED", True)
        assert release_lease(root, tx1, "owner-committed") == []

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        lease, errors = acquire_lease(root, tx1, "owner-a")
        assert lease is None
        assert errors == ["fixture_marker_missing"]

    print(
        "candidate_transaction_lease_fixture_smoke_ok exclusive=1 idempotent=1 "
        "owner_guard=1 transaction_guard=1 unfinished_journal_guard=1 "
        "recovered_release_guard=1 committed_release=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
