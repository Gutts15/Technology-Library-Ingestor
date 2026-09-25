#!/usr/bin/env python3
"""Smoke tests for the guarded disposable remote Git CAS/recovery probe."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_git_ref_remote_probe import PROBE_VERSION, REF_PREFIX, ls_remote, run_probe


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        remote = Path(temp) / "remote.git"
        created = subprocess.run(
            ["git", "init", "--bare", str(remote)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert created.returncode == 0, created.stderr

        report, errors = run_probe(str(remote))
        assert not errors, errors
        assert report is not None
        assert report["probe_version"] == PROBE_VERSION == "0.2.0"
        assert report["state"] == "PASS"
        assert report["single_winner"] is True
        assert report["winner"] in {"probe-owner-a", "probe-owner-b"}
        assert report["contender_a_succeeded"] != report["contender_b_succeeded"]
        assert report["restart_state_read"] is True
        assert report["recovery_verified"] is True
        assert report["recovered_from_owner"] == report["winner"]
        assert report["stale_owner_blocked"] is True
        assert report["restarted_owner_acquired"] is True
        assert report["owner_release_verified"] is True
        assert report["cleanup_verified"] is True
        assert report["cleanup_required"] is False
        assert report["production_publish_authorized"] is False
        assert report["canonical_write_performed"] is False
        assert report["fixture_ref"].startswith(REF_PREFIX)
        assert ls_remote(str(remote), report["fixture_ref"]) == ""

    print(
        "candidate_git_ref_remote_probe_smoke_ok disposable_ref=1 single_winner=1 "
        "restart_read=1 recovery_verified=1 stale_owner_blocked=1 "
        "owner_release_verified=1 cleanup_verified=1 production_authorization=0 "
        "canonical_write=0"
    )


if __name__ == "__main__":
    main()
