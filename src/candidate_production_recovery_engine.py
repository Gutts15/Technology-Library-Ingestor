#!/usr/bin/env python3
"""Deterministic production recovery engine for interrupted canonical transactions.

The engine is transport-agnostic. It consumes the durable production journal shape
created before canonical mutation and restores the exact pre-transaction state:

- paths that existed before are restored from SHA-verified snapshots;
- CREATE targets that did not exist before are deleted;
- affected indexes are restored exactly, including MASTER_INDEX.md;
- every restored/deleted path is verified;
- only then is a RECOVERED terminal journal produced.

This module performs no Git coordination and exposes no production CLI. The caller
must persist+readback the RECOVERED journal and release the coordinator only through
the dedicated verified-recovery transition.
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any, Callable

RECOVERY_ENGINE_VERSION = "0.1.0"

ReadBytes = Callable[[str], bytes | None]
WriteBytes = Callable[[str, bytes], bool]
DeletePath = Callable[[str], bool]


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _safe_canonical(path: str) -> bool:
    p = PurePosixPath(path.replace("\\", "/"))
    return (
        not p.is_absolute()
        and ".." not in p.parts
        and path.startswith("00_LIBRARY/")
    )


def _valid_sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in "0123456789abcdef" for ch in value)
    )


def validate_recovery_journal(journal: dict[str, Any] | None) -> list[str]:
    if not isinstance(journal, dict):
        return ["journal_invalid"]
    errors: list[str] = []

    if journal.get("canonical_write_performed") is not True:
        errors.append("journal_write_boundary_not_crossed")
    if journal.get("snapshots_verified") is not True:
        errors.append("journal_snapshots_not_verified")
    if journal.get("live_preconditions_verified_under_ownership") is not True:
        errors.append("journal_live_preconditions_not_verified")

    state = journal.get("state")
    if state not in {
        "WRITING_RECORDS",
        "REBUILDING_INDEXES",
        "VERIFYING",
        "COMMITTED_PENDING_RECEIPT",
        "RECOVERY_REQUIRED",
    }:
        errors.append("journal_state_not_recoverable")

    for key in ("transaction_id", "batch_id", "owner", "journal_path"):
        if not isinstance(journal.get(key), str) or not journal.get(key):
            errors.append(f"journal_{key}")

    master_sha = journal.get("master_index_before_sha256")
    if not _valid_sha(master_sha):
        errors.append("journal_master_before_sha")

    seen: set[str] = set()
    for group, path_key in (("items", "target_path"), ("indexes", "path")):
        rows = journal.get(group)
        if not isinstance(rows, list):
            errors.append(f"journal_{group}")
            continue
        for index, entry in enumerate(rows):
            if not isinstance(entry, dict):
                errors.append(f"{group}_{index}_invalid")
                continue
            path = entry.get(path_key)
            if not isinstance(path, str) or not _safe_canonical(path):
                errors.append(f"{group}_{index}_path")
                continue
            if path in seen:
                errors.append(f"duplicate_recovery_path:{path}")
            seen.add(path)

            existed = entry.get("existed_before")
            if not isinstance(existed, bool):
                errors.append(f"{group}_{index}_existed_before")
                continue
            before_sha = entry.get("before_sha256")
            snapshot_path = entry.get("snapshot_path")
            if existed:
                if not _valid_sha(before_sha):
                    errors.append(f"{group}_{index}_before_sha")
                if not isinstance(snapshot_path, str) or not snapshot_path.startswith(
                    "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/"
                ):
                    errors.append(f"{group}_{index}_snapshot_path")
            else:
                if before_sha is not None:
                    errors.append(f"{group}_{index}_unexpected_before_sha")
                if snapshot_path is not None:
                    errors.append(f"{group}_{index}_unexpected_snapshot")

    return sorted(set(errors))


def execute_recovery(
    journal: dict[str, Any] | None,
    *,
    read_canonical: ReadBytes,
    read_snapshot: ReadBytes,
    write_canonical: WriteBytes,
    delete_canonical: DeletePath,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors = validate_recovery_journal(journal)
    if errors or not isinstance(journal, dict):
        return None, errors or ["journal_invalid"]

    restored = 0
    removed = 0
    verified = 0

    ordered: list[tuple[str, dict[str, Any]]] = []
    for group, path_key in (("items", "target_path"), ("indexes", "path")):
        for entry in journal.get(group, []):
            ordered.append((path_key, entry))

    # Restore records first, indexes second, matching rollback contract.
    for path_key, entry in ordered:
        path = str(entry[path_key])
        if entry["existed_before"]:
            snapshot_path = str(entry["snapshot_path"])
            snapshot = read_snapshot(snapshot_path)
            if snapshot is None:
                return None, [f"snapshot_missing:{path}"]
            if sha256(snapshot) != entry["before_sha256"]:
                return None, [f"snapshot_sha_mismatch:{path}"]
            if not write_canonical(path, snapshot):
                return None, [f"restore_write_failed:{path}"]
            if read_canonical(path) != snapshot:
                return None, [f"restore_verify_failed:{path}"]
            restored += 1
            verified += 1
        else:
            if not delete_canonical(path):
                return None, [f"remove_created_failed:{path}"]
            if read_canonical(path) is not None:
                return None, [f"remove_created_verify_failed:{path}"]
            removed += 1
            verified += 1

    master = read_canonical("00_LIBRARY/MASTER_INDEX.md")
    if master is None:
        return None, ["master_restore_missing"]
    if sha256(master) != journal["master_index_before_sha256"]:
        return None, ["master_restore_sha_mismatch"]

    recovered = dict(journal)
    recovered.update(
        {
            "recovery_engine_version": RECOVERY_ENGINE_VERSION,
            "state": "RECOVERED",
            "rollback_verified": True,
            "restart_recovery_verified": True,
            "restored_paths": restored,
            "removed_created_paths": removed,
            "verified_paths": verified,
            "coordinator_release_allowed": True,
            "candidate_settlement_eligible": False,
            "canonical_write_performed": True,
        }
    )
    return recovered, []
