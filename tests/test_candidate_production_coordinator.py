#!/usr/bin/env python3
"""Smoke tests for the production Git exact-ref coordinator."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_production_coordinator import (
    acquire,
    ensure_initialized,
    read_state,
    release_prewrite_abort,
)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="tl-prod-coord-test-") as temp:
        bare = Path(temp) / "remote.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)

        sha, state, errors = ensure_initialized(str(bare))
        assert not errors and sha and state is not None, errors
        assert state["state"] == "FREE"

        active_sha, active, errors = acquire(
            str(bare),
            transaction_id="a" * 20,
            batch_id="b" * 20,
            owner="owner-one",
            journal_path="99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/" + "a" * 20 + "/owner-one/journal.json",
        )
        assert not errors and active_sha and active is not None, errors
        assert active["state"] == "ACTIVE"
        assert active["canonical_write_performed"] is False

        _, _, conflict = acquire(
            str(bare),
            transaction_id="c" * 20,
            batch_id="d" * 20,
            owner="owner-two",
            journal_path="99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/" + "c" * 20 + "/owner-two/journal.json",
        )
        assert "coordinator_busy" in conflict

        release_errors = release_prewrite_abort(
            str(bare),
            transaction_id="a" * 20,
            batch_id="b" * 20,
            owner="owner-one",
            journal_path="99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/" + "a" * 20 + "/owner-one/journal.json",
        )
        assert not release_errors, release_errors

        _, final, errors = read_state(str(bare))
        assert not errors and final is not None, errors
        assert final["state"] == "FREE"
        assert final["release_reason"] == "PREWRITE_ABORT_VERIFIED"

    print(
        "candidate_production_coordinator_smoke_ok exact_ref_cas=1 "
        "conflict_blocked=1 prewrite_release=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
