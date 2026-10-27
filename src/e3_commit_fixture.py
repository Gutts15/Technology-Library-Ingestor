#!/usr/bin/env python3
"""E3 complete synthetic-only committed transaction over local durable files.

This adapter connects EXISTING E3 preflight, Git CAS coordinator, durable
snapshots, canonical engine, verified receipt, committed journal and immutable
E1 terminal sidecar. It cannot access Google Drive or public canonical data:
the coordinator remote is a local bare Git repo beneath a marker-gated fixture.
A separate remote transport adapter is required before production use.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from candidate_canonical_transaction_engine import execute_canonical_transaction
from candidate_production_coordinator import (
    acquire, mark_canonical_write_started, read_state, release_committed,
)
from candidate_transaction_commit_engine import build_committed_journal
from candidate_transaction_receipt import build_receipt
from e1_lote_prepare import LOTES, encode
from e3_coordinator_fixture import (
    CoordinatorFixtureError, local_bare_repository, lock_identity, observed_active,
)
from e3_lote_state import related_paths, verify_settlement
from e3_recovery_fixture import (
    FixtureRecoveryError, guard, journal_path, load_journal, persist_journal,
    prepare_fixture, read_optional, sync_write,
)
from e3_terminal_receipt import SettlementError, build_terminal_receipt
from e3_transaction_preflight import compile_transaction


class CommitFixtureError(Exception):
    pass


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def durable_receipt(root: Path, journal: dict, filename: str, content: bytes) -> bytes:
    target = root / journal["run_root"] / filename
    if target.exists():
        if target.read_bytes() != content:
            raise CommitFixtureError("receipt_collision")
    else:
        sync_write(target, content, new_only=True)
    if target.read_bytes() != content:
        raise CommitFixtureError("receipt_readback_failed")
    return content


def assert_free_after_commit(git: str, expected: dict, receipt_sha: str) -> bool:
    _, state, errors = read_state(git)
    if errors or not isinstance(state, dict):
        raise CommitFixtureError("coordinator_uncertain")
    return (
        state.get("state") == "FREE"
        and state.get("release_reason") == "VERIFIED_COMMITTED"
        and state.get("released_transaction_id") == expected["transaction_id"]
        and state.get("released_batch_id") == expected["batch_id"]
        and state.get("released_receipt_sha256") == receipt_sha
    )


def release_committed_once(git: str, root: Path, expected: dict) -> None:
    journal = load_journal(root, expected["transaction_id"])
    if (journal.get("state") != "COMMITTED"
            or not journal.get("coordinator_release_allowed")
            or journal.get("transaction_id") != expected["transaction_id"]
            or journal.get("owner") != expected["owner"]):
        raise CommitFixtureError("commit_journal_not_verified")
    receipt_path = root / journal["run_root"] / "receipt.json"
    raw = receipt_path.read_bytes()
    sha_receipt = sha(raw)
    if sha_receipt != journal["receipt_sha256"]:
        raise CommitFixtureError("committed_receipt_changed")
    if assert_free_after_commit_if_available(git, expected, sha_receipt):
        return
    observed_active(git, expected, started=True)
    errors = release_committed(git, **expected, receipt_sha256=sha_receipt)
    if errors:
        # A lost Git ACK is not permission for an automatic second push.
        raise CommitFixtureError("commit_release_uncertain")
    if not assert_free_after_commit(git, expected, sha_receipt):
        raise CommitFixtureError("commit_release_unconfirmed")


def assert_free_after_commit_if_available(git: str, expected: dict, receipt_sha: str) -> bool:
    _, state, errors = read_state(git)
    if errors or not isinstance(state, dict):
        raise CommitFixtureError("coordinator_uncertain")
    if state["state"] == "FREE":
        if not assert_free_after_commit(git, expected, receipt_sha):
            raise CommitFixtureError("coordinator_release_other_transaction")
        return True
    return False


def terminal_sidecar(root: Path, relative_lot: str, raw_lot: bytes, raw_decisions: bytes,
                     journal: dict | None, receipt_raw: bytes | None) -> bytes:
    terminal = build_terminal_receipt(
        raw_lot, relative_lot, raw_decisions,
        commit_journal=journal,
        verified_receipt_raw=receipt_raw,
        read_canonical=lambda p: read_optional(root, p))
    _, sidecar_relative = related_paths(relative_lot)
    destination = root / LOTES / sidecar_relative
    raw = encode(terminal)
    if destination.exists():
        if destination.read_bytes() != raw:
            raise CommitFixtureError("terminal_receipt_collision")
    else:
        sync_write(destination, raw, new_only=True)
    verify_settlement(raw_lot, relative_lot, raw_decisions, destination.read_bytes())
    return raw


def exercise_commit(root: Path, git_repo: Path, raw_lot: bytes,
                    relative_lot: str, raw_decisions: bytes, *, fail_on_write=0
                    ) -> tuple[str, str | None]:
    remote = local_bare_repository(root, git_repo)
    with guard(root):
        plan, _, _, settlement = compile_transaction(
            root, raw_lot, relative_lot, raw_decisions)
        if not plan["items"]:
            terminal_sidecar(root, relative_lot, raw_lot, raw_decisions, None, None)
            return "NO_WRITE_SETTLED", None
        expected = lock_identity(plan["transaction_id"], settlement["lote_id"])
        _, active, errors = acquire(remote, **expected)
        if errors or active is None:
            raise CommitFixtureError("coordinator_acquire_blocked")
        observed_active(remote, expected, started=False)
        plan, staged, paths, journal = prepare_fixture(
            root, raw_lot, relative_lot, raw_decisions)
        if journal is None or journal["transaction_id"] != expected["transaction_id"]:
            raise CommitFixtureError("prepared_journal_invalid")
        observed_active(remote, expected, started=False)
        errors = mark_canonical_write_started(remote, **expected)
        if errors:
            raise CommitFixtureError("coordinator_start_uncertain")
        observed_active(remote, expected, started=True)
        journal["state"] = "WRITING_RECORDS"
        journal["canonical_write_performed"] = True
        persist_journal(root, journal)
        affected = {e["target_path"] for e in journal["items"]} | {
            e["path"] for e in journal["indexes"]}
        writes = 0

        def reader(p):
            return staged[p] if p in staged else read_optional(root, p)

        def write(p, b):
            nonlocal writes
            if p not in affected:
                return False
            writes += 1
            if writes == fail_on_write:
                return False
            sync_write(root / p, b)
            return read_optional(root, p) == b

        report, engine_errors = execute_canonical_transaction(
            plan, canonical_record_paths_before=paths, read_bytes=reader,
            write_record_bytes=write, write_index_bytes=write)
        if engine_errors or report.get("state") != "CANONICAL_STATE_VERIFIED":
            journal["state"] = "RECOVERY_REQUIRED"
            persist_journal(root, journal)
            return "RECOVERY_REQUIRED_OWNED", plan["transaction_id"]
        journal["state"] = "VERIFYING"
        persist_journal(root, journal)
        master_before = journal["master_index_before_sha256"]
        # Recover the exact original master from the verified durable snapshot.
        index_entry = next(e for e in journal["indexes"] if e["path"] == "00_LIBRARY/MASTER_INDEX.md")
        before_master = read_optional(root, index_entry["snapshot_path"])
        if before_master is None or sha(before_master) != master_before:
            raise CommitFixtureError("master_snapshot_changed")
        receipt, receipt_errors = build_receipt(
            plan, before_master, read_optional(root, "00_LIBRARY/MASTER_INDEX.md"), reader)
        if receipt_errors or receipt is None:
            journal["state"] = "RECOVERY_REQUIRED"
            persist_journal(root, journal)
            return "RECOVERY_REQUIRED_OWNED", plan["transaction_id"]
        receipt_raw = durable_receipt(root, journal, "receipt.json", encode(receipt))
        committed, commit_errors = build_committed_journal(journal, receipt, receipt_raw)
        if commit_errors or committed is None:
            raise CommitFixtureError("commit_journal_invalid")
        persist_journal(root, committed)
        terminal_sidecar(root, relative_lot, raw_lot, raw_decisions, committed, receipt_raw)
        release_committed_once(remote, root, expected)
        return "COMMITTED_RELEASED", plan["transaction_id"]


def resume_terminal_commit(root: Path, git_repo: Path, raw_lot: bytes,
                           relative_lot: str, raw_decisions: bytes) -> str:
    remote = local_bare_repository(root, git_repo)
    from e2_gemini_decide import validate_lote
    lote = validate_lote(raw_lot, relative_lot)
    with guard(root):
        _, state, errors = read_state(remote)
        if errors or not isinstance(state, dict):
            raise CommitFixtureError("coordinator_unavailable")
        tx_id = state.get("transaction_id") if state.get("state") == "ACTIVE" else state.get("released_transaction_id")
        if not isinstance(tx_id, str):
            raise CommitFixtureError("committed_transaction_unknown")
        journal = load_journal(root, tx_id)
        expected = lock_identity(tx_id, lote["lote_id"])
        if (journal.get("state") != "COMMITTED"
                or journal.get("readiness_sha256") != sha(raw_lot)
                or journal.get("authorization_sha256") != sha(raw_decisions)
                or journal.get("batch_id") != lote["lote_id"]):
            raise CommitFixtureError("not_committed_requires_recovery")
        receipt_raw = read_optional(root, journal["run_root"] + "/receipt.json")
        if receipt_raw is None or sha(receipt_raw) != journal["receipt_sha256"]:
            raise CommitFixtureError("receipt_invalid")
        terminal_sidecar(root, relative_lot, raw_lot, raw_decisions, journal, receipt_raw)
        release_committed_once(remote, root, expected)
        return "ALREADY_COMMITTED_RELEASED"
