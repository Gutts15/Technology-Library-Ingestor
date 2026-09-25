#!/usr/bin/env python3
"""Smoke tests for production recovery engine semantics."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_production_recovery_engine import execute_recovery


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def main() -> None:
    old_record = b"old record\n"
    old_master = b"# MASTER_INDEX\nold\n"
    old_index = b"# INDEX\nold\n"

    record = "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-existing.md"
    created = "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-created.md"
    master = "00_LIBRARY/MASTER_INDEX.md"
    domain_index = "00_LIBRARY/04_AI_AGENTS/INDEX.md"

    snapshots = {
        "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/x/record.bin": old_record,
        "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/x/master.bin": old_master,
        "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/x/index.bin": old_index,
    }
    store = {
        record: b"mutated record\n",
        created: b"created\n",
        master: b"# MASTER_INDEX\nnew\n",
        domain_index: b"# INDEX\nnew\n",
    }
    journal = {
        "state": "RECOVERY_REQUIRED",
        "transaction_id": "a" * 20,
        "batch_id": "b" * 20,
        "owner": "candidate-publisher-" + "c" * 12,
        "journal_path": "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/x/journal.json",
        "canonical_write_performed": True,
        "snapshots_verified": True,
        "live_preconditions_verified_under_ownership": True,
        "master_index_before_sha256": sha(old_master),
        "coordinator_release_allowed": False,
        "items": [
            {
                "target_path": record,
                "existed_before": True,
                "before_sha256": sha(old_record),
                "snapshot_path": "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/x/record.bin",
            },
            {
                "target_path": created,
                "existed_before": False,
                "before_sha256": None,
                "snapshot_path": None,
            },
        ],
        "indexes": [
            {
                "path": master,
                "existed_before": True,
                "before_sha256": sha(old_master),
                "snapshot_path": "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/x/master.bin",
            },
            {
                "path": domain_index,
                "existed_before": True,
                "before_sha256": sha(old_index),
                "snapshot_path": "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/x/index.bin",
            },
        ],
    }

    def read_canonical(path: str):
        return store.get(path)

    def read_snapshot(path: str):
        return snapshots.get(path)

    def write(path: str, raw: bytes) -> bool:
        store[path] = raw
        return True

    def delete(path: str) -> bool:
        store.pop(path, None)
        return True

    recovered, errors = execute_recovery(
        journal,
        read_canonical=read_canonical,
        read_snapshot=read_snapshot,
        write_canonical=write,
        delete_canonical=delete,
    )
    assert not errors, errors
    assert recovered is not None and recovered["state"] == "RECOVERED"
    assert recovered["rollback_verified"] is True
    assert recovered["coordinator_release_allowed"] is True
    assert recovered["candidate_settlement_eligible"] is False
    assert store[record] == old_record
    assert created not in store
    assert store[master] == old_master
    assert store[domain_index] == old_index

    # Corrupt snapshots must fail closed before claiming RECOVERED.
    broken = dict(snapshots)
    broken["99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/x/record.bin"] = b"corrupt\n"
    recovered, errors = execute_recovery(
        journal,
        read_canonical=read_canonical,
        read_snapshot=lambda path: broken.get(path),
        write_canonical=write,
        delete_canonical=delete,
    )
    assert recovered is None
    assert errors == [f"snapshot_sha_mismatch:{record}"], errors

    print(
        "candidate_production_recovery_engine_smoke_ok restore_update=1 remove_create=1 "
        "restore_indexes=1 master_sha=1 corrupt_snapshot_blocked=1"
    )


if __name__ == "__main__":
    main()
