#!/usr/bin/env python3
"""Smoke tests for exact expected-ref candidate coordination in local Git fixtures."""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_git_ref_coordination_fixture import (
    init_fixture,
    prepare_state_commit,
    push_expected,
    read_state,
    ref_sha,
)


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        remote = Path(temp) / "coord.git"
        base, errors = init_fixture(remote)
        assert not errors, errors
        assert base is not None
        assert read_state(remote)["state"] == "FREE"

        state_a = {"state": "ACTIVE", "transaction_id": "a" * 20, "owner": "owner-a"}
        state_b = {"state": "ACTIVE", "transaction_id": "b" * 20, "owner": "owner-b"}
        work_a, head_a, errors_a = prepare_state_commit(remote, base_sha=base, state=state_a)
        work_b, head_b, errors_b = prepare_state_commit(remote, base_sha=base, state=state_b)
        assert not errors_a and not errors_b
        assert work_a is not None and work_b is not None
        assert head_a is not None and head_b is not None and head_a != head_b
        try:
            won_a, _ = push_expected(work_a, remote, expected_sha=base)
            won_b, _ = push_expected(work_b, remote, expected_sha=base)
            assert won_a is True
            assert won_b is False
        finally:
            shutil.rmtree(work_a, ignore_errors=True)
            shutil.rmtree(work_b, ignore_errors=True)

        active_sha = ref_sha(remote)
        assert active_sha == head_a
        active = read_state(remote)
        assert active is not None
        assert active["state"] == "ACTIVE"
        assert active["transaction_id"] == "a" * 20
        assert active["owner"] == "owner-a"
        assert active["canonical_write_performed"] is False

        # A stale expected SHA remains rejected even when the attempted state is
        # otherwise reasonable. The ref itself is the compare-and-swap token.
        stale_state = {"state": "ACTIVE", "transaction_id": "c" * 20, "owner": "owner-c"}
        stale_work, _, stale_errors = prepare_state_commit(remote, base_sha=active_sha, state=stale_state)
        assert not stale_errors and stale_work is not None
        try:
            stale_won, _ = push_expected(stale_work, remote, expected_sha=base)
            assert stale_won is False
        finally:
            shutil.rmtree(stale_work, ignore_errors=True)
        assert ref_sha(remote) == active_sha

        # The current owner can produce the next state only against the exact
        # currently observed ref. This models a verified release transition.
        free_state = {"state": "FREE", "transaction_id": None, "owner": None}
        release_work, release_head, release_errors = prepare_state_commit(
            remote,
            base_sha=active_sha,
            state=free_state,
        )
        assert not release_errors and release_work is not None and release_head is not None
        try:
            released, _ = push_expected(release_work, remote, expected_sha=active_sha)
            assert released is True
        finally:
            shutil.rmtree(release_work, ignore_errors=True)
        assert ref_sha(remote) == release_head
        final = read_state(remote)
        assert final is not None and final["state"] == "FREE"

    print(
        "candidate_git_ref_coordination_fixture_smoke_ok single_winner=1 stale_ref_rejected=1 "
        "exact_transition=1 github_remote_unproven=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
