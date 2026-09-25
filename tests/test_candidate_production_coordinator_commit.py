#!/usr/bin/env python3
"""Smoke test for production coordinator commit-release transitions."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_production_coordinator import (
    acquire,
    mark_canonical_write_started,
    read_state,
    release_committed,
    release_prewrite_abort,
)


def git(args: list[str], cwd: Path | None = None):
    return subprocess.run(["git", *args], cwd=cwd, check=False, capture_output=True, text=True)


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        bare = root / "coord.git"
        assert git(["init", "--bare", str(bare)]).returncode == 0
        remote = str(bare)

        tx = "a" * 20
        batch = "b" * 20
        owner = "candidate-publisher-" + "c" * 12
        journal = "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/" + tx + "/" + owner + "/journal.json"

        _, active, errors = acquire(
            remote,
            transaction_id=tx,
            batch_id=batch,
            owner=owner,
            journal_path=journal,
        )
        assert not errors, errors
        assert active is not None and active["canonical_write_performed"] is False

        errors = mark_canonical_write_started(
            remote,
            transaction_id=tx,
            batch_id=batch,
            owner=owner,
            journal_path=journal,
        )
        assert not errors, errors

        _, active, errors = read_state(remote)
        assert not errors, errors
        assert active is not None and active["state"] == "ACTIVE"
        assert active["canonical_write_performed"] is True

        # Once the write boundary is crossed, prewrite-abort release must fail closed.
        errors = release_prewrite_abort(
            remote,
            transaction_id=tx,
            batch_id=batch,
            owner=owner,
            journal_path=journal,
        )
        assert errors == ["coordinator_release_identity_mismatch"], errors

        receipt_sha = "d" * 64
        errors = release_committed(
            remote,
            transaction_id=tx,
            batch_id=batch,
            owner=owner,
            journal_path=journal,
            receipt_sha256=receipt_sha,
        )
        assert not errors, errors

        _, free, errors = read_state(remote)
        assert not errors, errors
        assert free is not None and free["state"] == "FREE"
        assert free["release_reason"] == "VERIFIED_COMMITTED"
        assert free["released_receipt_sha256"] == receipt_sha

    print(
        "candidate_production_coordinator_commit_smoke_ok write_boundary=1 "
        "prewrite_release_blocked=1 committed_release=1 exact_ref_cas=1"
    )


if __name__ == "__main__":
    main()
