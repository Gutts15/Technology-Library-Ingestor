#!/usr/bin/env python3
"""E3 Git-coordinator + recovery integration in strictly local synthetic storage.

This module has NO production entry point. Its git origin must be an existing
local bare repository inside the fixture's temporary directory. It never
connects to github.com or writes to Google Drive.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

from candidate_production_coordinator import (
    acquire, mark_canonical_write_started, read_state, release_recovered,
)
from e1_lote_prepare import encode
from e3_recovery_fixture import (
    OWNER, digest, exercise_fixture, journal_path, load_journal,
    recover_fixture, verify_fixture,
)
from e3_transaction_preflight import compile_transaction


class CoordinatorFixtureError(Exception):
    pass


def local_bare_repository(root: Path, repository: Path) -> str:
    verify_fixture(root)
    base = root.resolve()
    candidate = repository.resolve()
    # Must be a separate local bare git repo, never a URL or remote production ref.
    if candidate == base or base not in candidate.parents or repository.is_symlink():
        raise CoordinatorFixtureError("non_fixture_git_remote")
    if not (candidate / "HEAD").is_file():
        raise CoordinatorFixtureError("bare_git_missing")
    try:
        command = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--is-bare-repository"],
            capture_output=True, text=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise CoordinatorFixtureError("bare_git_unavailable") from None
    if command.returncode or command.stdout.strip() != "true":
        raise CoordinatorFixtureError("non_bare_git_remote")
    return str(candidate)


def lock_identity(tx: str, batch: str) -> dict[str, str]:
    from candidate_transaction_prewrite_arm import recovery_run_root
    return {"transaction_id": tx, "batch_id": batch, "owner": OWNER,
            "journal_path": recovery_run_root(tx, OWNER) + "/journal.json"}


def observed_active(remote: str, expected: dict[str, str],
                    *, started: bool | None = None) -> dict:
    sha, state, errors = read_state(remote)
    if errors or not sha or state is None or state.get("state") != "ACTIVE":
        raise CoordinatorFixtureError("coordinator_owner_uncertain")
    if any(state.get(key) != value for key, value in expected.items()):
        raise CoordinatorFixtureError("coordinator_owner_mismatch")
    if started is not None and state.get("canonical_write_performed") is not started:
        raise CoordinatorFixtureError("coordinator_write_boundary_mismatch")
    return state


def checked_release(remote: str, root: Path, expected: dict[str, str]) -> None:
    state = observed_active(remote, expected, started=True)
    journal = load_journal(root, expected["transaction_id"])
    if (journal.get("state") != "RECOVERED"
            or journal.get("rollback_verified") is not True
            or journal.get("restart_recovery_verified") is not True
            or journal.get("coordinator_release_allowed") is not True
            or journal.get("snapshots_verified") is not True
            or journal.get("transaction_id") != expected["transaction_id"]
            or journal.get("batch_id") != expected["batch_id"]
            or journal.get("owner") != expected["owner"]
            or journal.get("journal_path") != expected["journal_path"]):
        raise CoordinatorFixtureError("recovery_not_durable")
    raw = encode(journal)
    if not journal_path(root, expected["transaction_id"]).is_file():
        raise CoordinatorFixtureError("journal_missing")
    if journal_path(root, expected["transaction_id"]).read_bytes() != raw:
        raise CoordinatorFixtureError("journal_bytes_unverified")
    sha = digest(raw)
    errors = release_recovered(remote, **expected, recovered_journal_sha256=sha)
    if errors:
        # May be a lost response after the Git push. Never retry the release.
        raise CoordinatorFixtureError("coordinator_release_uncertain")
    _, after, errors = read_state(remote)
    if (errors or after is None or after.get("state") != "FREE"
            or after.get("release_reason") != "VERIFIED_RECOVERED"
            or after.get("released_transaction_id") != expected["transaction_id"]
            or after.get("released_recovered_journal_sha256") != sha
            or after.get("generation", 0) <= state.get("generation", 0)):
        raise CoordinatorFixtureError("coordinator_release_unverified")


def run_fixture(root: Path, repository: Path, raw_lot: bytes,
                relative: str, raw_decisions: bytes, *, fail_on_write: int = 0
                ) -> tuple[str, str | None]:
    remote = local_bare_repository(root, repository)
    plan, _, _, result = compile_transaction(root, raw_lot, relative, raw_decisions)
    if not plan["items"]:
        return "NO_WRITE_DECISIONS", None
    expected = lock_identity(plan["transaction_id"], result["lote_id"])
    _, active, errors = acquire(remote, **expected)
    if errors or active is None:
        raise CoordinatorFixtureError("coordinator_acquire_failed")
    observed_active(remote, expected, started=False)

    def boundary(journal: dict[str, Any]) -> None:
        if (journal.get("state") != "PREPARED"
                or journal.get("snapshots_verified") is not True
                or journal.get("transaction_id") != expected["transaction_id"]
                or journal.get("journal_path") != expected["journal_path"]
                or journal.get("batch_id") != expected["batch_id"]):
            raise CoordinatorFixtureError("unarmed_journal")
        observed_active(remote, expected, started=False)
        errors = mark_canonical_write_started(remote, **expected)
        if errors:
            # Uncertain Git push must not authorize mutation or lease release.
            raise CoordinatorFixtureError("coordinator_start_uncertain")
        observed_active(remote, expected, started=True)

    outcome, transaction_id = exercise_fixture(
        root, raw_lot, relative, raw_decisions,
        fail_on_write=fail_on_write, before_canonical_write=boundary)
    if transaction_id != expected["transaction_id"]:
        raise CoordinatorFixtureError("transaction_changed")
    if outcome == "RECOVERED":
        checked_release(remote, root, expected)
        return "RECOVERED_RELEASED", transaction_id
    if outcome == "RECOVERY_REQUIRED":
        observed_active(remote, expected, started=True)
        return "RECOVERY_REQUIRED_OWNED", transaction_id
    raise CoordinatorFixtureError("unexpected_transaction_result")


def resume_fixture(root: Path, repository: Path,
                   transaction_id: str, batch_id: str) -> str:
    remote = local_bare_repository(root, repository)
    expected = lock_identity(transaction_id, batch_id)
    observed_active(remote, expected, started=True)
    journal = load_journal(root, transaction_id)
    if journal.get("state") == "PREPARED":
        # The coordinator may have crossed its write boundary without a
        # WRITING_RECORDS journal. No safe blanket release or rollback.
        raise CoordinatorFixtureError("uncertain_prewrite_boundary")
    if journal.get("state") == "RECOVERED":
        checked_release(remote, root, expected)
        return "ALREADY_RECOVERED_RELEASED"
    if journal.get("state") not in {"RECOVERY_REQUIRED", "WRITING_RECORDS", "VERIFYING"}:
        raise CoordinatorFixtureError("journal_requires_manual_reconciliation")
    outcome = recover_fixture(root, transaction_id)
    if outcome not in {"RECOVERED", "ALREADY_RECOVERED"}:
        raise CoordinatorFixtureError("rollback_not_verified")
    checked_release(remote, root, expected)
    return "RECOVERED_RELEASED"
