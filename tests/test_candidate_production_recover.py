#!/usr/bin/env python3
"""Smoke contracts for fresh-process production recovery CLI."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_production_recover import (
    RECOVERY_CLI_VERSION,
    classify_restart_action,
    verify_recovered_state,
)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def coordinator(write_started: bool) -> dict:
    return {
        "state": "ACTIVE",
        "transaction_id": "a" * 20,
        "batch_id": "b" * 20,
        "owner": "candidate-publisher-123456789abc",
        "journal_path": (
            "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/"
            + "a" * 20
            + "/candidate-publisher-123456789abc/journal.json"
        ),
        "canonical_write_performed": write_started,
    }


def journal(write_started: bool, state: str) -> dict:
    return {
        **coordinator(write_started),
        "state": state,
        "canonical_write_performed": write_started,
    }


def main() -> None:
    assert RECOVERY_CLI_VERSION == "0.1.0"

    action, errors = classify_restart_action(coordinator(False), journal(False, "PREPARED"))
    assert not errors and action == "PREWRITE_ABORT"

    action, errors = classify_restart_action(
        coordinator(True),
        journal(True, "RECOVERY_REQUIRED"),
    )
    assert not errors and action == "RECOVER"

    recovered = journal(True, "RECOVERED")
    recovered["rollback_verified"] = True
    recovered["coordinator_release_allowed"] = True
    action, errors = classify_restart_action(coordinator(True), recovered)
    assert not errors and action == "RELEASE_RECOVERED"

    staged = journal(True, "WRITING_RECORDS")
    action, errors = classify_restart_action(coordinator(False), staged)
    assert not errors and action == "PREWRITE_ABORT"

    mismatch = journal(False, "PREPARED")
    action, errors = classify_restart_action(coordinator(True), mismatch)
    assert action is None and "write_boundary_mismatch" in errors

    before = b"before\n"
    master = b"master-before\n"
    recovery_journal = {
        "master_index_before_sha256": sha256(master),
        "items": [
            {
                "target_path": "00_LIBRARY/11_GAMING/2_TOOL/technology-existing.md",
                "existed_before": True,
                "before_sha256": sha256(before),
            },
            {
                "target_path": "00_LIBRARY/11_GAMING/2_TOOL/technology-created.md",
                "existed_before": False,
                "before_sha256": None,
            },
        ],
        "indexes": [
            {
                "path": "00_LIBRARY/MASTER_INDEX.md",
                "existed_before": True,
                "before_sha256": sha256(master),
            }
        ],
    }
    current = {
        "00_LIBRARY/11_GAMING/2_TOOL/technology-existing.md": before,
        "00_LIBRARY/MASTER_INDEX.md": master,
    }
    errors = verify_recovered_state(recovery_journal, current.get)
    assert not errors, errors

    current["00_LIBRARY/11_GAMING/2_TOOL/technology-created.md"] = b"unexpected"
    errors = verify_recovered_state(recovery_journal, current.get)
    assert "recovered_created_path_still_present:00_LIBRARY/11_GAMING/2_TOOL/technology-created.md" in errors

    print(
        "candidate_production_recover_smoke_ok "
        "prewrite_abort=1 staged_boundary_abort=1 recovery=1 "
        "recovered_release=1 exact_restore_verify=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
