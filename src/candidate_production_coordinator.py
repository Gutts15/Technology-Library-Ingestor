#!/usr/bin/env python3
"""Production Git exact-ref coordinator for candidate canonical transactions.

This module owns only a dedicated Git ref and a tiny JSON state file. It never reads
or writes Technology Library canonical records. State transitions use an exact
observed-ref compare-and-swap push so concurrent owners cannot silently overwrite one
another.

The first supported production transition is intentionally narrow:
FREE -> ACTIVE -> FREE for a verified pre-write abort. Canonical mutation/recovery
transitions are added only in later executor stages.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
COORDINATOR_VERSION = "0.1.0"
COORDINATOR_REF = "refs/heads/tl-candidate-production-coordinator"
STATE_PATH = ".candidate-production-coordinator.json"


def git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        check=False,
        capture_output=True,
        text=True,
    )


def ls_remote(remote_url: str) -> str | None:
    result = git(["ls-remote", remote_url, COORDINATOR_REF])
    if result.returncode != 0:
        return None
    line = result.stdout.strip()
    if not line:
        return ""
    parts = line.split()
    return parts[0] if parts else ""


def _configure(work: Path) -> None:
    git(["config", "user.name", "Technology Library Production Coordinator"], work)
    git(["config", "user.email", "production-coordinator@example.invalid"], work)


def _payload(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "coordinator_version": COORDINATOR_VERSION,
        "production_coordinator": True,
        **state,
    }


def _write_state(work: Path, state: dict[str, Any]) -> None:
    (work / STATE_PATH).write_text(
        json.dumps(_payload(state), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def validate_state(payload: dict[str, Any] | None) -> list[str]:
    if not isinstance(payload, dict):
        return ["coordinator_state_invalid"]
    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("coordinator_schema")
    if payload.get("coordinator_version") != COORDINATOR_VERSION:
        errors.append("coordinator_version")
    if payload.get("production_coordinator") is not True:
        errors.append("coordinator_production_flag")
    if payload.get("state") not in {"FREE", "ACTIVE"}:
        errors.append("coordinator_state")
    if not isinstance(payload.get("generation"), int) or int(payload.get("generation")) < 0:
        errors.append("coordinator_generation")
    if not isinstance(payload.get("canonical_write_performed"), bool):
        errors.append("coordinator_write_flag")
    if payload.get("state") == "ACTIVE":
        for key in ("transaction_id", "batch_id", "owner", "journal_path"):
            if not isinstance(payload.get(key), str) or not payload.get(key):
                errors.append(f"coordinator_active_{key}")
    return sorted(set(errors))


def read_state(remote_url: str) -> tuple[str | None, dict[str, Any] | None, list[str]]:
    observed = ls_remote(remote_url)
    if observed is None:
        return None, None, ["coordinator_remote_unreachable"]
    if not observed:
        return "", None, ["coordinator_ref_missing"]

    with tempfile.TemporaryDirectory(prefix="tl-production-coordinator-read-") as temp:
        work = Path(temp)
        if git(["init"], work).returncode != 0:
            return None, None, ["coordinator_read_init_failed"]
        if git(["remote", "add", "origin", remote_url], work).returncode != 0:
            return None, None, ["coordinator_read_remote_failed"]
        if git(["fetch", "--no-tags", "origin", COORDINATOR_REF], work).returncode != 0:
            return None, None, ["coordinator_read_fetch_failed"]
        fetched = git(["rev-parse", "FETCH_HEAD"], work)
        if fetched.returncode != 0 or fetched.stdout.strip() != observed:
            return None, None, ["coordinator_read_sha_changed"]
        shown = git(["show", f"FETCH_HEAD:{STATE_PATH}"], work)
        if shown.returncode != 0:
            return None, None, ["coordinator_read_state_missing"]
        try:
            payload = json.loads(shown.stdout)
        except json.JSONDecodeError:
            return None, None, ["coordinator_read_invalid_json"]

    errors = validate_state(payload if isinstance(payload, dict) else None)
    return (observed, payload if isinstance(payload, dict) else None, errors)


def ensure_initialized(remote_url: str) -> tuple[str | None, dict[str, Any] | None, list[str]]:
    if not shutil.which("git"):
        return None, None, ["git_missing"]
    existing = ls_remote(remote_url)
    if existing is None:
        return None, None, ["coordinator_remote_unreachable"]
    if existing:
        return read_state(remote_url)

    with tempfile.TemporaryDirectory(prefix="tl-production-coordinator-seed-") as temp:
        work = Path(temp)
        if git(["init"], work).returncode != 0:
            return None, None, ["coordinator_seed_init_failed"]
        _configure(work)
        _write_state(
            work,
            {
                "state": "FREE",
                "transaction_id": None,
                "batch_id": None,
                "owner": None,
                "journal_path": None,
                "generation": 0,
                "canonical_write_performed": False,
                "release_reason": None,
            },
        )
        if git(["add", STATE_PATH], work).returncode != 0:
            return None, None, ["coordinator_seed_add_failed"]
        if git(["commit", "-m", "coordination: initialize production coordinator"], work).returncode != 0:
            return None, None, ["coordinator_seed_commit_failed"]
        pushed = git(["push", remote_url, f"HEAD:{COORDINATOR_REF}"], work)

    if pushed.returncode != 0:
        # A concurrent initializer may have won. Accept only a valid existing state.
        sha, state, errors = read_state(remote_url)
        if not errors and sha and state is not None:
            return sha, state, []
        return None, None, ["coordinator_seed_push_failed", *errors]
    return read_state(remote_url)


def _prepare_transition(
    remote_url: str,
    base_sha: str,
    state: dict[str, Any],
    *,
    message: str,
) -> tuple[Path | None, str | None, list[str]]:
    temp = Path(tempfile.mkdtemp(prefix="tl-production-coordinator-state-"))
    if git(["init"], temp).returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["coordinator_state_init_failed"]
    _configure(temp)
    if git(["remote", "add", "origin", remote_url], temp).returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["coordinator_state_remote_failed"]
    if git(["fetch", "--no-tags", "origin", COORDINATOR_REF], temp).returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["coordinator_state_fetch_failed"]
    observed = git(["rev-parse", "FETCH_HEAD"], temp)
    if observed.returncode != 0 or observed.stdout.strip() != base_sha:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["coordinator_state_base_changed"]
    if git(["checkout", "--detach", base_sha], temp).returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["coordinator_state_checkout_failed"]
    _write_state(temp, state)
    if git(["add", STATE_PATH], temp).returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["coordinator_state_add_failed"]
    if git(["commit", "-m", message], temp).returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["coordinator_state_commit_failed"]
    head = git(["rev-parse", "HEAD"], temp)
    if head.returncode != 0 or not head.stdout.strip():
        shutil.rmtree(temp, ignore_errors=True)
        return None, None, ["coordinator_state_head_failed"]
    return temp, head.stdout.strip(), []


def _push_expected(work: Path, expected_sha: str) -> bool:
    result = git(
        [
            "push",
            f"--force-with-lease={COORDINATOR_REF}:{expected_sha}",
            "origin",
            f"HEAD:{COORDINATOR_REF}",
        ],
        work,
    )
    return result.returncode == 0


def acquire(
    remote_url: str,
    *,
    transaction_id: str,
    batch_id: str,
    owner: str,
    journal_path: str,
) -> tuple[str | None, dict[str, Any] | None, list[str]]:
    base_sha, current, errors = ensure_initialized(remote_url)
    if errors or not base_sha or current is None:
        return None, None, errors or ["coordinator_initialize_failed"]

    if current.get("state") == "ACTIVE":
        same_owner = (
            current.get("transaction_id") == transaction_id
            and current.get("batch_id") == batch_id
            and current.get("owner") == owner
            and current.get("journal_path") == journal_path
        )
        if same_owner:
            return base_sha, current, []
        return None, None, ["coordinator_busy"]
    if current.get("state") != "FREE":
        return None, None, ["coordinator_not_free"]

    next_state = {
        "state": "ACTIVE",
        "transaction_id": transaction_id,
        "batch_id": batch_id,
        "owner": owner,
        "journal_path": journal_path,
        "generation": int(current.get("generation") or 0) + 1,
        "canonical_write_performed": False,
        "release_reason": None,
    }
    work, head, prep_errors = _prepare_transition(
        remote_url,
        base_sha,
        next_state,
        message=f"coordination: acquire {transaction_id} by {owner}",
    )
    if prep_errors or work is None or head is None:
        return None, None, prep_errors or ["coordinator_acquire_prepare_failed"]
    try:
        if not _push_expected(work, base_sha):
            return None, None, ["coordinator_acquire_conflict"]
    finally:
        shutil.rmtree(work, ignore_errors=True)

    observed_sha, observed, read_errors = read_state(remote_url)
    if read_errors or observed_sha != head or observed != _payload(next_state):
        return None, None, read_errors or ["coordinator_acquire_verify_failed"]
    return observed_sha, observed, []


def release_prewrite_abort(
    remote_url: str,
    *,
    transaction_id: str,
    batch_id: str,
    owner: str,
    journal_path: str,
) -> list[str]:
    base_sha, current, errors = read_state(remote_url)
    if errors or not base_sha or current is None:
        return errors or ["coordinator_release_read_failed"]
    expected = (
        current.get("state") == "ACTIVE"
        and current.get("transaction_id") == transaction_id
        and current.get("batch_id") == batch_id
        and current.get("owner") == owner
        and current.get("journal_path") == journal_path
        and current.get("canonical_write_performed") is False
    )
    if not expected:
        return ["coordinator_release_identity_mismatch"]

    next_state = {
        "state": "FREE",
        "transaction_id": None,
        "batch_id": None,
        "owner": None,
        "journal_path": None,
        "generation": int(current.get("generation") or 0) + 1,
        "canonical_write_performed": False,
        "release_reason": "PREWRITE_ABORT_VERIFIED",
        "released_by_owner": owner,
        "released_transaction_id": transaction_id,
        "released_batch_id": batch_id,
        "released_journal_path": journal_path,
    }
    work, head, prep_errors = _prepare_transition(
        remote_url,
        base_sha,
        next_state,
        message=f"coordination: verified prewrite abort {transaction_id}",
    )
    if prep_errors or work is None or head is None:
        return prep_errors or ["coordinator_release_prepare_failed"]
    try:
        if not _push_expected(work, base_sha):
            return ["coordinator_release_conflict"]
    finally:
        shutil.rmtree(work, ignore_errors=True)

    observed_sha, observed, read_errors = read_state(remote_url)
    if read_errors or observed_sha != head or observed != _payload(next_state):
        return read_errors or ["coordinator_release_verify_failed"]
    return []


def mark_canonical_write_started(
    remote_url: str,
    *,
    transaction_id: str,
    batch_id: str,
    owner: str,
    journal_path: str,
) -> list[str]:
    """Mark the exact ACTIVE owner as having crossed the canonical write boundary."""
    base_sha, current, errors = read_state(remote_url)
    if errors or not base_sha or current is None:
        return errors or ["coordinator_write_start_read_failed"]
    expected = (
        current.get("state") == "ACTIVE"
        and current.get("transaction_id") == transaction_id
        and current.get("batch_id") == batch_id
        and current.get("owner") == owner
        and current.get("journal_path") == journal_path
    )
    if not expected:
        return ["coordinator_write_start_identity_mismatch"]
    if current.get("canonical_write_performed") is True:
        return []

    next_state = {
        **{k: v for k, v in current.items() if k not in {"schema_version", "coordinator_version", "production_coordinator"}},
        "state": "ACTIVE",
        "generation": int(current.get("generation") or 0) + 1,
        "canonical_write_performed": True,
        "release_reason": None,
    }
    work, head, prep_errors = _prepare_transition(
        remote_url,
        base_sha,
        next_state,
        message=f"coordination: canonical write started {transaction_id}",
    )
    if prep_errors or work is None or head is None:
        return prep_errors or ["coordinator_write_start_prepare_failed"]
    try:
        if not _push_expected(work, base_sha):
            return ["coordinator_write_start_conflict"]
    finally:
        shutil.rmtree(work, ignore_errors=True)

    observed_sha, observed, read_errors = read_state(remote_url)
    if read_errors or observed_sha != head or observed != _payload(next_state):
        return read_errors or ["coordinator_write_start_verify_failed"]
    return []


def release_committed(
    remote_url: str,
    *,
    transaction_id: str,
    batch_id: str,
    owner: str,
    journal_path: str,
    receipt_sha256: str,
) -> list[str]:
    """Release an ACTIVE production owner only after verified committed state."""
    if (
        not isinstance(receipt_sha256, str)
        or len(receipt_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in receipt_sha256)
    ):
        return ["coordinator_receipt_sha_invalid"]

    base_sha, current, errors = read_state(remote_url)
    if errors or not base_sha or current is None:
        return errors or ["coordinator_commit_release_read_failed"]
    expected = (
        current.get("state") == "ACTIVE"
        and current.get("transaction_id") == transaction_id
        and current.get("batch_id") == batch_id
        and current.get("owner") == owner
        and current.get("journal_path") == journal_path
        and current.get("canonical_write_performed") is True
    )
    if not expected:
        return ["coordinator_commit_release_identity_mismatch"]

    next_state = {
        "state": "FREE",
        "transaction_id": None,
        "batch_id": None,
        "owner": None,
        "journal_path": None,
        "generation": int(current.get("generation") or 0) + 1,
        "canonical_write_performed": False,
        "release_reason": "VERIFIED_COMMITTED",
        "released_by_owner": owner,
        "released_transaction_id": transaction_id,
        "released_batch_id": batch_id,
        "released_journal_path": journal_path,
        "released_receipt_sha256": receipt_sha256,
    }
    work, head, prep_errors = _prepare_transition(
        remote_url,
        base_sha,
        next_state,
        message=f"coordination: verified commit {transaction_id}",
    )
    if prep_errors or work is None or head is None:
        return prep_errors or ["coordinator_commit_release_prepare_failed"]
    try:
        if not _push_expected(work, base_sha):
            return ["coordinator_commit_release_conflict"]
    finally:
        shutil.rmtree(work, ignore_errors=True)

    observed_sha, observed, read_errors = read_state(remote_url)
    if read_errors or observed_sha != head or observed != _payload(next_state):
        return read_errors or ["coordinator_commit_release_verify_failed"]
    return []


def release_recovered(
    remote_url: str,
    *,
    transaction_id: str,
    batch_id: str,
    owner: str,
    journal_path: str,
    recovered_journal_sha256: str,
) -> list[str]:
    """Release an ACTIVE owner only after a verified RECOVERED terminal journal."""
    if (
        not isinstance(recovered_journal_sha256, str)
        or len(recovered_journal_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in recovered_journal_sha256)
    ):
        return ["coordinator_recovered_journal_sha_invalid"]

    base_sha, current, errors = read_state(remote_url)
    if errors or not base_sha or current is None:
        return errors or ["coordinator_recovery_release_read_failed"]
    expected = (
        current.get("state") == "ACTIVE"
        and current.get("transaction_id") == transaction_id
        and current.get("batch_id") == batch_id
        and current.get("owner") == owner
        and current.get("journal_path") == journal_path
        and current.get("canonical_write_performed") is True
    )
    if not expected:
        return ["coordinator_recovery_release_identity_mismatch"]

    next_state = {
        "state": "FREE",
        "transaction_id": None,
        "batch_id": None,
        "owner": None,
        "journal_path": None,
        "generation": int(current.get("generation") or 0) + 1,
        "canonical_write_performed": False,
        "release_reason": "VERIFIED_RECOVERED",
        "released_by_owner": owner,
        "released_transaction_id": transaction_id,
        "released_batch_id": batch_id,
        "released_journal_path": journal_path,
        "released_recovered_journal_sha256": recovered_journal_sha256,
    }
    work, head, prep_errors = _prepare_transition(
        remote_url,
        base_sha,
        next_state,
        message=f"coordination: verified recovery {transaction_id}",
    )
    if prep_errors or work is None or head is None:
        return prep_errors or ["coordinator_recovery_release_prepare_failed"]
    try:
        if not _push_expected(work, base_sha):
            return ["coordinator_recovery_release_conflict"]
    finally:
        shutil.rmtree(work, ignore_errors=True)

    observed_sha, observed, read_errors = read_state(remote_url)
    if read_errors or observed_sha != head or observed != _payload(next_state):
        return read_errors or ["coordinator_recovery_release_verify_failed"]
    return []
