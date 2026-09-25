#!/usr/bin/env python3
"""Persist, retire and recover a durable rollback journal only inside local test fixtures.

The normal fixture transaction tests prove in-process rollback. This module covers
the nastier failure mode: the process disappears after one or more fixture writes
and therefore cannot execute its exception handler. Before writes begin, a durable
journal snapshots every affected record plus the exact set/bytes of generated
indexes that existed at transaction start. A later invocation can restore that
state byte-for-byte. On successful publication simulation, the same journal may be
retired only after a verified transaction receipt exists.

This is deliberately fixture-only. It requires the explicit Technology Library
fixture marker, has no rclone/remote mode, and never writes the real 00_LIBRARY.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_transaction_fixture import (
    FIXTURE_MARKER,
    FIXTURE_MARKER_VALUE,
    index_paths,
    validate_plan,
)
from candidate_transaction_plan import read_json
from candidate_transaction_receipt import RECEIPT_VERSION

SCHEMA_VERSION = 1
RECOVERY_VERSION = "0.2.0"
RECOVERY_ROOT = ".candidate-transaction-recovery"
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024


class RecoveryFailure(RuntimeError):
    pass


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def require_fixture(root: Path) -> None:
    marker = root / FIXTURE_MARKER
    try:
        value = marker.read_text(encoding="utf-8")
    except OSError as exc:
        raise RecoveryFailure("fixture_marker_missing") from exc
    if value != FIXTURE_MARKER_VALUE:
        raise RecoveryFailure("fixture_marker_invalid")


def safe_relative(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise RecoveryFailure("unsafe_snapshot_path")
    return path.as_posix().strip("/")


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _write_atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="transaction-recovery-", dir=path.parent, delete=False) as handle:
        handle.write(raw)
        temp = Path(handle.name)
    temp.replace(path)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_atomic(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def journal_dir(root: Path, transaction_id: str) -> Path:
    return root / RECOVERY_ROOT / transaction_id


def _snapshot_entry(root: Path, journal: Path, relative: str) -> tuple[dict[str, Any], int]:
    relative = safe_relative(relative)
    raw = _read(root / relative)
    entry: dict[str, Any] = {
        "path": relative,
        "existed": raw is not None,
        "sha256": sha256(raw) if raw is not None else None,
        "snapshot_path": None,
    }
    if raw is None:
        return entry, 0
    snapshot_rel = str(PurePosixPath("snapshots") / PurePosixPath(relative))
    _write_atomic(journal / snapshot_rel, raw)
    entry["snapshot_path"] = snapshot_rel
    return entry, len(raw)


def prepare_recovery_journal(
    root: Path,
    plan: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    root = root.resolve()
    try:
        require_fixture(root)
    except RecoveryFailure as exc:
        return None, [str(exc)]

    items, errors = validate_plan(plan)
    if errors or not isinstance(plan, dict):
        return None, errors or ["transaction_plan_invalid"]
    transaction_id = plan.get("transaction_id")
    if not isinstance(transaction_id, str) or len(transaction_id) != 20:
        return None, ["transaction_id_invalid"]

    master_path = root / "00_LIBRARY/MASTER_INDEX.md"
    master_before = _read(master_path)
    if master_before is None:
        return None, ["master_missing"]
    if sha256(master_before) != plan.get("master_index_sha256"):
        return None, ["master_sha_changed"]

    target_paths = sorted(
        {
            safe_relative(str(item["target_path"]))
            for item in items
            if item.get("action") in {"CREATE", "UPDATE"}
        }
    )
    old_indexes = sorted(index_paths(root))
    snapshot_paths = sorted(set(target_paths) | set(old_indexes))

    jdir = journal_dir(root, transaction_id)
    journal_path = jdir / "journal.json"
    existing = read_json(journal_path) if journal_path.exists() else None
    if existing is not None:
        if (
            existing.get("schema_version") == SCHEMA_VERSION
            and existing.get("recovery_version") == RECOVERY_VERSION
            and existing.get("transaction_id") == transaction_id
            and existing.get("state") in {"PREPARED", "RECOVERED", "COMMITTED"}
            and existing.get("master_index_before_sha256") == sha256(master_before)
        ):
            return existing, []
        return None, ["recovery_journal_conflict"]

    entries: list[dict[str, Any]] = []
    total_bytes = 0
    try:
        for relative in snapshot_paths:
            entry, size = _snapshot_entry(root, jdir, relative)
            total_bytes += size
            if total_bytes > MAX_SNAPSHOT_BYTES:
                raise RecoveryFailure("snapshot_too_large")
            entries.append(entry)
    except (OSError, RecoveryFailure) as exc:
        code = str(exc) if isinstance(exc, RecoveryFailure) else "snapshot_write_failed"
        return None, [code]

    journal = {
        "schema_version": SCHEMA_VERSION,
        "recovery_version": RECOVERY_VERSION,
        "transaction_id": transaction_id,
        "state": "PREPARED",
        "master_index_before_sha256": sha256(master_before),
        "master_index_after_sha256": None,
        "receipt_version": None,
        "receipt_verified": False,
        "old_index_paths": old_indexes,
        "target_paths": target_paths,
        "entries": entries,
        "snapshot_bytes": total_bytes,
        "rollback_verified": False,
        "fixture_only": True,
        "canonical_write_performed": False,
    }
    try:
        _write_json_atomic(journal_path, journal)
    except OSError:
        return None, ["journal_write_failed"]
    stored = read_json(journal_path)
    if stored != journal:
        return None, ["journal_verify_failed"]
    return journal, []


def mark_journal_committed(
    root: Path,
    transaction_id: str,
    receipt: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Retire rollback intent only after a verified receipt proves commit success."""

    root = root.resolve()
    try:
        require_fixture(root)
    except RecoveryFailure as exc:
        return None, [str(exc)]
    if not isinstance(transaction_id, str) or len(transaction_id) != 20:
        return None, ["transaction_id_invalid"]

    path = journal_dir(root, transaction_id) / "journal.json"
    journal = read_json(path)
    if not isinstance(journal, dict):
        return None, ["recovery_journal_missing"]
    if journal.get("schema_version") != SCHEMA_VERSION or journal.get("recovery_version") != RECOVERY_VERSION:
        return None, ["recovery_journal_version"]
    if journal.get("transaction_id") != transaction_id:
        return None, ["recovery_transaction_id"]
    if journal.get("state") == "COMMITTED":
        if journal.get("receipt_verified") is True:
            return journal, []
        return None, ["committed_without_verified_receipt"]
    if journal.get("state") != "PREPARED":
        return None, ["recovery_journal_not_prepared"]

    errors: list[str] = []
    if not isinstance(receipt, dict):
        errors.append("receipt_invalid")
    else:
        if receipt.get("schema_version") != SCHEMA_VERSION:
            errors.append("receipt_schema")
        if receipt.get("receipt_version") != RECEIPT_VERSION:
            errors.append("receipt_version")
        if receipt.get("transaction_id") != transaction_id:
            errors.append("receipt_transaction_id")
        if receipt.get("state") != "VERIFIED_COMMITTED_STATE":
            errors.append("receipt_state")
        if receipt.get("candidate_settlement_eligible") is not True:
            errors.append("receipt_not_settlement_eligible")
        if receipt.get("verification_only") is not True:
            errors.append("receipt_verification_flag")
        if receipt.get("canonical_write_performed") is not False:
            errors.append("receipt_write_flag")
        if receipt.get("master_index_before_sha256") != journal.get("master_index_before_sha256"):
            errors.append("receipt_master_before_mismatch")
        after_sha = receipt.get("master_index_after_sha256")
        if not isinstance(after_sha, str) or len(after_sha) != 64:
            errors.append("receipt_master_after_sha")
    if errors:
        return None, sorted(set(errors))

    committed = dict(journal)
    committed["state"] = "COMMITTED"
    committed["master_index_after_sha256"] = receipt["master_index_after_sha256"]
    committed["receipt_version"] = receipt["receipt_version"]
    committed["receipt_verified"] = True
    committed["rollback_verified"] = False
    try:
        _write_json_atomic(path, committed)
    except OSError:
        return None, ["journal_commit_write_failed"]
    if read_json(path) != committed:
        return None, ["journal_commit_verify_failed"]
    return committed, []


def recover_from_journal(
    root: Path,
    transaction_id: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    root = root.resolve()
    try:
        require_fixture(root)
    except RecoveryFailure as exc:
        return None, [str(exc)]
    if not isinstance(transaction_id, str) or len(transaction_id) != 20:
        return None, ["transaction_id_invalid"]

    jdir = journal_dir(root, transaction_id)
    journal_path = jdir / "journal.json"
    journal = read_json(journal_path)
    if not isinstance(journal, dict):
        return None, ["recovery_journal_missing"]
    errors: list[str] = []
    if journal.get("schema_version") != SCHEMA_VERSION:
        errors.append("recovery_schema")
    if journal.get("recovery_version") != RECOVERY_VERSION:
        errors.append("recovery_version")
    if journal.get("transaction_id") != transaction_id:
        errors.append("recovery_transaction_id")
    if journal.get("fixture_only") is not True:
        errors.append("recovery_fixture_flag")
    if journal.get("canonical_write_performed") is not False:
        errors.append("recovery_write_flag")
    if journal.get("state") not in {"PREPARED", "RECOVERED"}:
        errors.append("recovery_state")
    entries = journal.get("entries")
    old_index_paths = journal.get("old_index_paths")
    if not isinstance(entries, list):
        errors.append("recovery_entries")
        entries = []
    if not isinstance(old_index_paths, list) or not all(isinstance(path, str) for path in old_index_paths):
        errors.append("recovery_old_indexes")
        old_index_paths = []
    if errors:
        return None, sorted(set(errors))

    # Any generated index that appeared after the snapshot is transaction debris.
    for relative in sorted(index_paths(root) - set(old_index_paths)):
        try:
            (root / relative).unlink()
        except OSError:
            errors.append(f"recovery_delete_new_index_failed:{relative}")

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"recovery_entry_{index}_invalid")
            continue
        try:
            relative = safe_relative(str(entry.get("path") or ""))
        except RecoveryFailure:
            errors.append(f"recovery_entry_{index}_path")
            continue
        existed = entry.get("existed")
        target = root / relative
        if existed is False:
            try:
                if target.exists():
                    target.unlink()
            except OSError:
                errors.append(f"recovery_delete_failed:{relative}")
            continue
        if existed is not True:
            errors.append(f"recovery_entry_{index}_existed")
            continue
        snapshot_rel = entry.get("snapshot_path")
        expected_sha = entry.get("sha256")
        if not isinstance(snapshot_rel, str) or not isinstance(expected_sha, str):
            errors.append(f"recovery_entry_{index}_snapshot")
            continue
        try:
            snapshot_rel = safe_relative(snapshot_rel)
        except RecoveryFailure:
            errors.append(f"recovery_entry_{index}_snapshot_path")
            continue
        raw = _read(jdir / snapshot_rel)
        if raw is None or sha256(raw) != expected_sha:
            errors.append(f"recovery_entry_{index}_snapshot_sha")
            continue
        try:
            _write_atomic(target, raw)
        except OSError:
            errors.append(f"recovery_restore_failed:{relative}")

    # Byte-for-byte verification after restore.
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        relative = entry["path"]
        current = _read(root / relative)
        if entry.get("existed") is False:
            if current is not None:
                errors.append(f"recovery_verify_absence_failed:{relative}")
        elif entry.get("existed") is True:
            if current is None or sha256(current) != entry.get("sha256"):
                errors.append(f"recovery_verify_sha_failed:{relative}")

    if errors:
        failed = dict(journal)
        failed["state"] = "RECOVERY_FAILED"
        failed["rollback_verified"] = False
        try:
            _write_json_atomic(journal_path, failed)
        except OSError:
            pass
        return failed, sorted(set(errors))

    recovered = dict(journal)
    recovered["state"] = "RECOVERED"
    recovered["rollback_verified"] = True
    recovered["receipt_verified"] = False
    try:
        _write_json_atomic(journal_path, recovered)
    except OSError:
        return None, ["recovery_state_write_failed"]
    return recovered, []


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare, commit or recover a durable transaction journal in a local fixture only.")
    parser.add_argument("--root-dir", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", type=Path, help="transaction plan JSON")
    mode.add_argument("--recover", help="transaction ID")
    mode.add_argument("--commit-receipt", type=Path, help="verified receipt JSON")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if args.prepare is not None:
        journal, errors = prepare_recovery_journal(args.root_dir, read_json(args.prepare))
        action = "prepared"
    elif args.commit_receipt is not None:
        receipt = read_json(args.commit_receipt)
        transaction_id = receipt.get("transaction_id") if isinstance(receipt, dict) else None
        journal, errors = mark_journal_committed(args.root_dir, str(transaction_id or ""), receipt)
        action = "committed"
    else:
        journal, errors = recover_from_journal(args.root_dir, str(args.recover))
        action = "recovered"
    if args.out is not None and journal is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(journal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if errors or journal is None:
        state = journal.get("state") if isinstance(journal, dict) else "FAILED"
        print(
            "candidate_transaction_recovery_fixture_error "
            f"action={action} state={state} codes={','.join(errors or ['failed'])} canonical_write=0"
        )
        return 2
    print(
        "candidate_transaction_recovery_fixture_ok "
        f"action={action} transaction_id={journal['transaction_id']} state={journal['state']} "
        f"rollback_verified={1 if journal.get('rollback_verified') else 0} "
        f"receipt_verified={1 if journal.get('receipt_verified') else 0} canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
