#!/usr/bin/env python3
"""E3 confirmed-COMMITTED coordinator release reconciliation.

Complements the existing controlled publisher. A verified COMMITTED journal is
NEVER a rollback candidate. Only a committed receipt, matching canonical
bytes and exact Git owner permit one verified release; a lost Git ACK is
reconciled by read-only inspection, never by repeating the push.

Pure verification is intentionally independent from storage transports.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any, Callable

SHA = re.compile(r"^[0-9a-f]{64}$")
ID = re.compile(r"^[0-9a-f]{20}$")
OWNER = re.compile(r"^candidate-publisher-[0-9a-f]{12}$")
ROOT = "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/"
MASTER = "00_LIBRARY/MASTER_INDEX.md"

Read = Callable[[str], bytes | None]
ReadOwner = Callable[[], tuple[str | None, dict | None, list[str]]]
Release = Callable[..., list[str]]


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def safe_canonical(path: Any) -> bool:
    if not isinstance(path, str) or "\\" in path or "//" in path:
        return False
    p = PurePosixPath(path)
    return path.startswith("00_LIBRARY/") and not p.is_absolute() and ".." not in p.parts and path.endswith(".md")


def proof_errors(journal: dict | None, receipt_raw: bytes,
                 *, read_canonical: Read, read_private: Read) -> list[str]:
    """Verify every durable byte without changing the store."""
    if not isinstance(journal, dict):
        return ["commit_journal_invalid"]
    tx, owner, batch, run_root = (journal.get(k) for k in
                                  ("transaction_id", "owner", "batch_id", "run_root"))
    if (not isinstance(tx, str) or not ID.fullmatch(tx)
            or not isinstance(owner, str) or not OWNER.fullmatch(owner)
            or not isinstance(batch, str) or not batch
            or run_root != ROOT + tx + "/" + owner
            or journal.get("journal_path") != run_root + "/journal.json"
            or journal.get("state") != "COMMITTED"
            or journal.get("canonical_write_performed") is not True
            or journal.get("snapshots_verified") is not True
            or journal.get("live_preconditions_verified_under_ownership") is not True
            or journal.get("coordinator_release_allowed") is not True
            or journal.get("candidate_settlement_eligible") is not True
            or not isinstance(journal.get("receipt_sha256"), str)
            or not SHA.fullmatch(journal["receipt_sha256"])):
        return ["commit_journal_unverified"]
    if (not isinstance(receipt_raw, bytes) or not 0 < len(receipt_raw) <= 128 * 1024
            or digest(receipt_raw) != journal["receipt_sha256"]):
        return ["committed_receipt_hash_mismatch"]
    try:
        receipt = json.loads(receipt_raw.decode("utf-8"),
                             object_pairs_hook=_unique_pairs)
    except (UnicodeError, ValueError, TypeError):
        return ["committed_receipt_json_invalid"]
    if (not isinstance(receipt, dict)
            or receipt.get("state") != "VERIFIED_COMMITTED_STATE"
            or receipt.get("transaction_id") != tx
            or receipt.get("verification_only") is not True
            or receipt.get("candidate_settlement_eligible") is not True
            or receipt.get("canonical_write_performed") is not False
            or receipt.get("master_index_before_sha256") != journal.get("master_index_before_sha256")
            or not isinstance(receipt.get("master_index_after_sha256"), str)
            or not SHA.fullmatch(receipt["master_index_after_sha256"])):
        return ["committed_receipt_identity_invalid"]
    rows, indexes, receipt_rows = journal.get("items"), journal.get("indexes"), receipt.get("items")
    if (not isinstance(rows, list) or not 1 <= len(rows) <= 50
            or not isinstance(indexes, list) or not 1 <= len(indexes) <= 100
            or not isinstance(receipt_rows, list) or len(receipt_rows) != len(rows)):
        return ["commit_inventory_invalid"]
    receipt_by_path: dict[str, dict] = {}
    for r in receipt_rows:
        if not isinstance(r, dict) or not safe_canonical(r.get("target_path")):
            return ["committed_receipt_items_invalid"]
        path = r["target_path"]
        if path in receipt_by_path:
            return ["committed_receipt_duplicate"]
        receipt_by_path[path] = r
    seen: set[str] = set()
    has_master = False
    for group, key in ((rows, "target_path"), (indexes, "path")):
        for entry in group:
            if not isinstance(entry, dict) or not safe_canonical(entry.get(key)):
                return ["commit_path_invalid"]
            path = entry[key]
            if path in seen:
                return ["commit_paths_duplicate"]
            seen.add(path)
            has_master |= path == MASTER
            after = entry.get("after_sha256")
            before = entry.get("before_sha256")
            if not isinstance(after, str) or not SHA.fullmatch(after):
                return ["commit_after_hash_missing"]
            if entry.get("existed_before") is True:
                snapshot_path = entry.get("snapshot_path")
                if (not isinstance(before, str) or not SHA.fullmatch(before)
                        or not isinstance(snapshot_path, str)
                        or not snapshot_path.startswith(run_root + "/snapshots/")
                        or ".." in PurePosixPath(snapshot_path).parts):
                    return ["commit_before_snapshot_invalid"]
                snapshot = read_private(snapshot_path)
                if snapshot is None or digest(snapshot) != before:
                    return ["commit_snapshot_changed"]
            elif (entry.get("existed_before") is not False
                  or before is not None or entry.get("snapshot_path") is not None):
                return ["commit_create_prestate_invalid"]
            current = read_canonical(path)
            if current is None or digest(current) != after:
                return ["commit_canonical_mismatch"]
            if key == "target_path":
                receipt_item = receipt_by_path.get(path)
                if (receipt_item is None
                        or receipt_item.get("content_sha256") != after
                        or receipt_item.get("action") != entry.get("action")
                        or receipt_item.get("record_id") != entry.get("record_id")):
                    return ["commit_receipt_item_unbound"]
            if path == MASTER and (after != receipt["master_index_after_sha256"]
                                   or before != receipt["master_index_before_sha256"]):
                return ["commit_master_binding_invalid"]
    if not has_master or set(receipt_by_path) != {row["target_path"] for row in rows}:
        return ["commit_index_or_items_incomplete"]
    return []


def _unique_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate_json_key")
        out[key] = value
    return out


def reconcile_committed_release(journal: dict, receipt_raw: bytes, *,
                                read_canonical: Read, read_private: Read,
                                read_owner: ReadOwner, release_once: Release
                                ) -> tuple[str, list[str]]:
    errors = proof_errors(journal, receipt_raw, read_canonical=read_canonical,
                          read_private=read_private)
    if errors:
        return "BLOCKED", errors
    _, owner, read_errors = read_owner()
    if read_errors or not isinstance(owner, dict):
        return "UNCERTAIN", ["coordinator_unavailable"]
    fields = ("transaction_id", "batch_id", "owner", "journal_path")
    identity = {k: journal[k] for k in fields}
    receipt_sha = digest(receipt_raw)
    if owner.get("state") == "FREE":
        if (owner.get("release_reason") == "VERIFIED_COMMITTED"
                and owner.get("released_receipt_sha256") == receipt_sha
                and all(owner.get("released_by_owner" if k == "owner" else "released_" + k) == v
                        for k, v in identity.items())):
            return "ALREADY_RELEASED", []
        return "BLOCKED", ["coordinator_released_foreign_transaction"]
    if (owner.get("state") != "ACTIVE"
            or owner.get("canonical_write_performed") is not True
            or any(owner.get(k) != v for k, v in identity.items())):
        return "BLOCKED", ["coordinator_active_identity_mismatch"]

    failure = release_once(**identity, receipt_sha256=receipt_sha)
    # Whether the Git push returned success or a lost ACK, perform only a
    # read-only reconciliation. Never blindly repeat a state transition.
    _, final, verify_errors = read_owner()
    if verify_errors or not isinstance(final, dict):
        return "UNCERTAIN", ["commit_release_readback_unavailable"]
    if (final.get("state") == "FREE"
            and final.get("release_reason") == "VERIFIED_COMMITTED"
            and final.get("released_receipt_sha256") == receipt_sha
            and all(final.get("released_by_owner" if k == "owner" else "released_" + k) == v
                    for k, v in identity.items())):
        return "RELEASED", []
    return "UNCERTAIN", ["commit_release_unconfirmed"]


def classify_restart(journal: dict | None, coordinator: dict | None, *,
                     read_canonical: Read, read_snapshot: Read,
                     receipt_raw: bytes | None = None) -> tuple[str, list[str]]:
    """Pure state-machine router for the next owner session; makes no writes.

    Returning a route does not itself release Git or restore data. The caller
    must use the existing verified commit/prewrite/recovery engines.
    """
    from candidate_production_recovery_engine import verify_rollback_preconditions
    if not isinstance(journal, dict) or not isinstance(coordinator, dict):
        return "BLOCKED", ["restart_evidence_unavailable"]
    keys = ("transaction_id", "batch_id", "owner", "journal_path")
    if not all(isinstance(journal.get(k), str) and journal[k] for k in keys):
        return "BLOCKED", ["restart_journal_identity_invalid"]
    state = journal.get("state")
    if state == "COMMITTED":
        if receipt_raw is None:
            return "BLOCKED", ["committed_receipt_unavailable"]
        errors = proof_errors(journal, receipt_raw,
                              read_canonical=read_canonical, read_private=read_snapshot)
        if errors:
            return "BLOCKED", errors
        if coordinator.get("state") == "FREE":
            result, mismatch = reconcile_committed_release(
                journal, receipt_raw, read_canonical=read_canonical,
                read_private=read_snapshot,
                read_owner=lambda: (None, coordinator, []),
                release_once=lambda **_: (_ for _ in ()).throw(
                    AssertionError("read_only_classifier_cannot_release")),
            )
            return ("ALREADY_RELEASED", []) if result == "ALREADY_RELEASED" else ("BLOCKED", mismatch)
        if (coordinator.get("state") == "ACTIVE"
                and coordinator.get("canonical_write_performed") is True
                and all(coordinator.get(k) == journal[k] for k in keys)):
            return "RECONCILE_COMMITTED", []
        return "BLOCKED", ["restart_commit_owner_mismatch"]
    if (coordinator.get("state") != "ACTIVE"
            or any(coordinator.get(k) != journal[k] for k in keys)):
        return "BLOCKED", ["restart_owner_mismatch"]
    armed = coordinator.get("canonical_write_performed")
    if state == "PREPARED":
        if armed is not False or journal.get("snapshots_verified") is not True:
            return "BLOCKED", ["restart_prewrite_uncertain"]
        if not _canonical_equals_before(journal, read_canonical, read_snapshot):
            return "BLOCKED", ["restart_prewrite_content_changed"]
        return "SAFE_PREWRITE_ABORT", []
    if state in {
        "WRITING_RECORDS", "REBUILDING_INDEXES", "VERIFYING",
        "COMMITTED_PENDING_RECEIPT", "RECOVERY_REQUIRED",
    }:
        if armed is not True:
            return "BLOCKED", ["restart_write_boundary_unconfirmed"]
        problems = verify_rollback_preconditions(
            journal, read_canonical=read_canonical, read_snapshot=read_snapshot)
        if problems:
            return "BLOCKED", problems
        return "RECOVER_UNCOMMITTED", []
    if state == "RECOVERED":
        if (armed is not True or journal.get("rollback_verified") is not True
                or journal.get("restart_recovery_verified") is not True
                or journal.get("coordinator_release_allowed") is not True
                or not _canonical_equals_before(journal, read_canonical, read_snapshot)):
            return "BLOCKED", ["restart_recovered_state_unverified"]
        return "RELEASE_RECOVERED", []
    return "BLOCKED", ["restart_journal_state_unsupported"]


def _canonical_equals_before(journal: dict, read_canonical: Read,
                             read_snapshot: Read) -> bool:
    if journal.get("snapshots_verified") is not True:
        return False
    rows = journal.get("items")
    indexes = journal.get("indexes")
    if not isinstance(rows, list) or not isinstance(indexes, list) or not rows or not indexes:
        return False
    touched = set()
    for group, key in ((rows, "target_path"), (indexes, "path")):
        for entry in group:
            if not isinstance(entry, dict) or not safe_canonical(entry.get(key)):
                return False
            path = entry[key]
            if path in touched:
                return False
            touched.add(path)
            before = entry.get("before_sha256")
            current = read_canonical(path)
            if entry.get("existed_before") is True:
                snap = entry.get("snapshot_path")
                if (not isinstance(before, str) or not SHA.fullmatch(before)
                        or not isinstance(snap, str)
                        or not snap.startswith(str(journal.get("run_root")) + "/snapshots/")
                        or (content := read_snapshot(snap)) is None
                        or digest(content) != before
                        or current is None or digest(current) != before):
                    return False
            elif (entry.get("existed_before") is not False
                  or before is not None or current is not None):
                return False
    return MASTER in touched and digest(read_canonical(MASTER) or b"") == journal.get("master_index_before_sha256")


def reconcile_live(remote: str, git_remote: str, journal_path: str) -> tuple[str, list[str]]:
    """Privileged call: only for a trusted run already holding approved access."""
    from candidate_remote_recovery_probe import remote_cat
    from candidate_production_coordinator import read_state, release_committed
    if (not isinstance(journal_path, str) or not journal_path.startswith(ROOT)
            or ".." in PurePosixPath(journal_path).parts or not journal_path.endswith("/journal.json")):
        return "BLOCKED", ["journal_path_invalid"]
    raw = remote_cat(remote, journal_path)
    if raw is None or len(raw) > 128 * 1024:
        return "UNCERTAIN", ["committed_journal_unavailable"]
    try:
        journal = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, ValueError):
        return "BLOCKED", ["committed_journal_invalid"]
    if (not isinstance(journal, dict) or journal.get("journal_path") != journal_path
            or not isinstance(journal.get("run_root"), str)):
        return "BLOCKED", ["journal_identity_invalid"]
    receipt_raw = remote_cat(remote, journal["run_root"] + "/receipt.json")
    if receipt_raw is None:
        return "UNCERTAIN", ["committed_receipt_unavailable"]
    return reconcile_committed_release(
        journal, receipt_raw,
        read_canonical=lambda p: remote_cat(remote, p),
        read_private=lambda p: remote_cat(remote, p),
        read_owner=lambda: read_state(git_remote),
        release_once=lambda **kwargs: release_committed(git_remote, **kwargs),
    )
