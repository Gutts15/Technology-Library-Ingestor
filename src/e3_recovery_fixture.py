#!/usr/bin/env python3
"""E3 synthetic-only durable snapshots and rollback exercise.

Requires a private synthetic fixture marker. No remote mode, no Actions hook,
no production canonical write, no live Gemini calls. This exercises the
existing transaction and recovery engines against local synthetic files.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from candidate_canonical_transaction_engine import execute_canonical_transaction
from candidate_production_recovery_engine import execute_recovery
from candidate_transaction_prewrite_arm import (
    affected_index_paths, build_prepared_journal, recovery_run_root,
)
from e1_lote_prepare import LOTES, encode
from e3_transaction_preflight import compile_transaction

MARKER = "99_INBOX/CURATION/_synthetic-e3-fixture.marker"
MARKER_BYTES = b"E3_SYNTHETIC_ONLY\n"
OWNER = "candidate-publisher-" + "0" * 12
SHA = re.compile(r"^[0-9a-f]{20}$")
CANONICAL = re.compile(r"^00_LIBRARY/(?!.*(?:\.\.|//))[A-Za-z0-9_/-]+\.md$")
JOURNAL_MAX = 100 * 1024


class FixtureRecoveryError(Exception):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def safe(root: Path, path: str) -> Path:
    if (not isinstance(path, str) or path.startswith("/") or "\\" in path
            or ".." in Path(path).parts or "//" in path):
        raise FixtureRecoveryError("unsafe_path")
    return root / path


def read_optional(root: Path, relative: str):
    p = safe(root, relative)
    return p.read_bytes() if p.is_file() else None


def sync_write(path: Path, raw: bytes, *, new_only=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if new_only:
        with path.open("xb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
    else:
        fd, temp_name = tempfile.mkstemp(prefix=".e3-fixture-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    if path.read_bytes() != raw:
        raise FixtureRecoveryError("durable_readback_failed")


def verify_fixture(root: Path):
    if read_optional(root, MARKER) != MARKER_BYTES:
        raise FixtureRecoveryError("synthetic_marker_missing")


@contextmanager
def guard(root: Path):
    verify_fixture(root)
    path = root / "99_INBOX/CURATION/.e3-fixture.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def journal_path(root: Path, transaction_id: str) -> Path:
    if not SHA.fullmatch(transaction_id):
        raise FixtureRecoveryError("transaction_id_invalid")
    return safe(root, recovery_run_root(transaction_id, OWNER) + "/journal.json")


def persist_journal(root, journal):
    raw = encode(journal)
    if len(raw) > JOURNAL_MAX:
        raise FixtureRecoveryError("journal_oversize")
    target = journal_path(root, journal["transaction_id"])
    sync_write(target, raw)
    if json.loads(target.read_bytes()) != journal:
        raise FixtureRecoveryError("journal_readback_invalid")


def load_journal(root, transaction_id):
    target = journal_path(root, transaction_id)
    try:
        raw = target.read_bytes()
        if len(raw) > JOURNAL_MAX:
            raise ValueError()
        journal = json.loads(raw)
    except (OSError, ValueError, UnicodeError):
        raise FixtureRecoveryError("journal_missing_or_invalid") from None
    if not isinstance(journal, dict) or journal.get("transaction_id") != transaction_id:
        raise FixtureRecoveryError("journal_identity_invalid")
    return journal


def prepare_fixture(root: Path, raw_lot: bytes, relative: str, raw_decisions: bytes):
    plan, content, existing_paths, settlement = compile_transaction(
        root, raw_lot, relative, raw_decisions)
    if not plan["items"]:
        return None, {}, [], None
    transaction_id = plan["transaction_id"]
    if journal_path(root, transaction_id).exists():
        raise FixtureRecoveryError("existing_journal_requires_reconciliation")
    plan = dict(plan, finalize_batch_id=settlement["lote_id"])
    affected = affected_index_paths(plan)
    targets = {item["target_path"] for item in plan["items"]}
    if targets.intersection(affected):
        raise FixtureRecoveryError("path_overlap_invalid")
    touched = sorted(targets | set(affected))
    if any(not CANONICAL.fullmatch(path) for path in touched):
        raise FixtureRecoveryError("noncanonical_target")
    before = {p: read_optional(root, p) for p in touched}
    old_master = before.get("00_LIBRARY/MASTER_INDEX.md")
    if old_master is None or digest(old_master) != plan["master_index_sha256"]:
        raise FixtureRecoveryError("master_changed_prewrite")
    # Simulate the real existing engine to bind every expected post-state.
    preview = dict(content)
    def dry_read(p):
        return preview.get(p, read_optional(root, p))
    def dry_write(p, raw):
        if p not in touched:
            return False
        preview[p] = raw
        return True
    report, errors = execute_canonical_transaction(
        plan, canonical_record_paths_before=existing_paths, read_bytes=dry_read,
        write_record_bytes=dry_write, write_index_bytes=dry_write)
    if errors or report["state"] != "CANONICAL_STATE_VERIFIED":
        raise FixtureRecoveryError("prewrite_simulation_failed")
    run_root = recovery_run_root(transaction_id, OWNER)
    journal = build_prepared_journal(
        transaction=plan, readiness_raw=raw_lot,
        authorization_raw=raw_decisions, transaction_raw=encode(plan),
        owner=OWNER, run_root=run_root, before_state=before)
    for group, key in (("items", "target_path"), ("indexes", "path")):
        for entry in journal[group]:
            p = entry[key]
            after = preview.get(p, read_optional(root, p))
            entry["after_sha256"] = digest(after) if after is not None else None
            prior = before[p]
            if prior is None:
                continue
            snap = entry["snapshot_path"]
            if not snap:
                raise FixtureRecoveryError("snapshot_identity_invalid")
            sync_write(safe(root, snap), prior, new_only=True)
            if read_optional(root, snap) != prior:
                raise FixtureRecoveryError("snapshot_readback_failed")
    if any(read_optional(root, p) != before[p] for p in touched):
        raise FixtureRecoveryError("canonical_changed_during_prepare")
    journal["snapshots_verified"] = True
    journal["fixture_only"] = True
    journal["state"] = "PREPARED"
    persist_journal(root, journal)
    return plan, content, existing_paths, journal


def recover_fixture(root: Path, transaction_id: str):
    journal = load_journal(root, transaction_id)
    if not journal.get("fixture_only"):
        raise FixtureRecoveryError("not_synthetic")
    if journal["state"] == "RECOVERED":
        return "ALREADY_RECOVERED"
    if journal["state"] == "PREPARED":
        # No canonical mutation is allowed until the ARMED journal is durable.
        return "PREPARED_NOT_MUTATED"
    if journal["state"] not in {"WRITING_RECORDS", "VERIFYING", "RECOVERY_REQUIRED"}:
        raise FixtureRecoveryError("unreconciled_journal_state")
    rows = [(entry["target_path"], entry) for entry in journal["items"]]
    rows += [(entry["path"], entry) for entry in journal["indexes"]]
    if not rows or len({p for p, _ in rows}) != len(rows):
        raise FixtureRecoveryError("recovery_inventory_invalid")
    # Do not override edits by another writer: only before/expected-after
    # bytes are safe to restore under this local exclusive fixture lock.
    for path, entry in rows:
        raw = read_optional(root, path)
        actual = digest(raw) if raw is not None else None
        before_sha, after_sha = entry["before_sha256"], entry.get("after_sha256")
        if actual not in {before_sha, after_sha}:
            raise FixtureRecoveryError("recovery_external_conflict")
    def write_record(p, raw):
        if p not in {key for key, _ in rows}:
            return False
        sync_write(safe(root, p), raw)
        return True
    def delete_record(p):
        if p not in {key for key, _ in rows}:
            return False
        file = safe(root, p)
        if file.exists():
            file.unlink()
            folder = os.open(file.parent, os.O_RDONLY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
        return not file.exists()
    recovered, errors = execute_recovery(
        journal, read_canonical=lambda p: read_optional(root, p),
        read_snapshot=lambda p: read_optional(root, p),
        write_canonical=write_record, delete_canonical=delete_record)
    if errors or recovered is None:
        raise FixtureRecoveryError("verified_recovery_failed")
    recovered["fixture_only"] = True
    persist_journal(root, recovered)
    return "RECOVERED"


def exercise_fixture(root: Path, raw_lot: bytes, relative: str,
                     raw_decisions: bytes, *, fail_on_write: int = 0,
                     before_canonical_write=None):
    with guard(root):
        plan, staged, before_paths, journal = prepare_fixture(
            root, raw_lot, relative, raw_decisions)
        if plan is None:
            return "NO_WRITE_DECISIONS", None
        tx_id = journal["transaction_id"]
        touched = {e["target_path"] for e in journal["items"]}
        touched.update(e["path"] for e in journal["indexes"])
        # The callback may bind the exact active Git coordinator lease before
        # the durable write-start journal and before any canonical mutation.
        # A failed/uncertain callback aborts without releasing the owner.
        if before_canonical_write is not None:
            before_canonical_write(journal)
        journal["state"] = "WRITING_RECORDS"
        journal["canonical_write_performed"] = True
        persist_journal(root, journal)
        writes = 0
        def reader(p):
            return staged[p] if p in staged else read_optional(root, p)
        def writer(p, raw):
            nonlocal writes
            if p not in touched:
                return False
            writes += 1
            if writes == fail_on_write:
                return False
            sync_write(safe(root, p), raw)
            return True
        report, errors = execute_canonical_transaction(
            plan, canonical_record_paths_before=before_paths,
            read_bytes=reader, write_record_bytes=writer,
            write_index_bytes=writer)
        if errors or report["state"] != "CANONICAL_STATE_VERIFIED":
            journal["state"] = "RECOVERY_REQUIRED"
            persist_journal(root, journal)
            return "RECOVERY_REQUIRED", tx_id
        journal["state"] = "VERIFYING"
        persist_journal(root, journal)
        # This is a reversible local exercise, never a production commit.
        return recover_fixture(root, tx_id), tx_id


def main():
    parser = argparse.ArgumentParser(description="E3 synthetic rollback exercise only")
    parser.add_argument("--root-dir", required=True, type=Path)
    parser.add_argument("--date-brt", required=True)
    parser.add_argument("--resume-transaction")
    args = parser.parse_args()
    root = args.root_dir.resolve()
    try:
        with guard(root):
            if args.resume_transaction:
                status = recover_fixture(root, args.resume_transaction)
            else:
                day = args.date_brt
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                    raise FixtureRecoveryError("invalid_day")
                relative = day + "/lote.json"
                lot = (root / LOTES / relative).read_bytes()
                decisions = (root / LOTES / day / "decisoes.json").read_bytes()
                # Avoid the nested lock in exercise_fixture by releasing
                # this lock before it calls guard itself.
                status = None
        if status is None:
            status, _ = exercise_fixture(root, lot, relative, decisions)
    except (FixtureRecoveryError, OSError, ValueError):
        print("e3_fixture_error code=blocked canonical_production_write=0")
        return 2
    print("e3_fixture status=" + status + " canonical_production_write=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
