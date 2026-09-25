#!/usr/bin/env python3
"""Smoke tests for production pre-write recovery planning."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_transaction_prewrite_arm import (
    affected_index_paths,
    build_prepared_journal,
    recovery_run_root,
    safe_recovery_path,
)


def main() -> None:
    transaction = {
        "transaction_id": "a" * 20,
        "finalize_batch_id": "b" * 20,
        "master_index_sha256": "c" * 64,
        "items": [
            {
                "action": "CREATE",
                "record_id": "d" * 20,
                "target_path": "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-example.md",
            },
            {
                "action": "CREATE",
                "record_id": "e" * 20,
                "target_path": "00_LIBRARY/SOURCES/source-example.md",
            },
        ],
    }
    owner = "candidate-publisher-" + "f" * 12
    run_root = recovery_run_root("a" * 20, owner)
    indexes = affected_index_paths(transaction)
    assert indexes == [
        "00_LIBRARY/04_AI_AGENTS/INDEX.md",
        "00_LIBRARY/MASTER_INDEX.md",
        "00_LIBRARY/SOURCES/INDEX.md",
    ]

    before = {
        "00_LIBRARY/MASTER_INDEX.md": b"master-before\n",
        "00_LIBRARY/04_AI_AGENTS/INDEX.md": b"ai-before\n",
        "00_LIBRARY/SOURCES/INDEX.md": b"sources-before\n",
        "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-example.md": None,
        "00_LIBRARY/SOURCES/source-example.md": None,
    }
    journal = build_prepared_journal(
        transaction=transaction,
        readiness_raw=b"readiness\n",
        authorization_raw=b"authorization\n",
        transaction_raw=b"transaction\n",
        owner=owner,
        run_root=run_root,
        before_state=before,
    )
    assert journal["state"] == "PREPARED"
    assert journal["canonical_write_performed"] is False
    assert journal["live_preconditions_verified_under_ownership"] is True
    assert all(
        entry["snapshot_path"] is None
        for entry in journal["items"]
        if entry["action"] == "CREATE"
    )
    assert all(
        safe_recovery_path(entry["snapshot_path"])
        for entry in journal["indexes"]
        if entry["snapshot_path"] is not None
    )
    assert safe_recovery_path(journal["journal_path"])
    assert not safe_recovery_path("00_LIBRARY/MASTER_INDEX.md")
    assert not safe_recovery_path("99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/../escape")

    print(
        "candidate_transaction_prewrite_arm_smoke_ok affected_indexes=3 "
        "create_absence_bound=1 private_scope=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
