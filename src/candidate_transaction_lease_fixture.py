#!/usr/bin/env python3
"""Model exclusive candidate transaction ownership only in a local test fixture.

The future production publisher must prevent two canonical transactions from
running at once. This fixture uses local O_EXCL creation to exercise ownership,
idempotent reacquisition by the same owner and conflict behavior. A stale owner
left behind by a simulated process crash may be cleared only after the matching
durable recovery journal proves exact restoration.

Once a recovery journal exists, even the original owner may not release the lease
until that journal is either COMMITTED by a verified receipt or RECOVERED with
verified rollback. This prevents a partially mutated transaction from abandoning
its ownership marker and letting another transaction start.

It is not a remote locking implementation and deliberately has no rclone mode.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from candidate_transaction_fixture import FIXTURE_MARKER, FIXTURE_MARKER_VALUE
from candidate_transaction_plan import read_json
from candidate_transaction_recovery_fixture import RECOVERY_VERSION, journal_dir

SCHEMA_VERSION = 1
LEASE_VERSION = "0.3.0"
LEASE_DIR = ".candidate-transaction-lease"
LEASE_FILE = "active.json"
ID_RE = re.compile(r"^[0-9a-f]{20}$")
OWNER_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class LeaseFailure(RuntimeError):
    pass


def require_fixture(root: Path) -> None:
    try:
        value = (root / FIXTURE_MARKER).read_text(encoding="utf-8")
    except OSError as exc:
        raise LeaseFailure("fixture_marker_missing") from exc
    if value != FIXTURE_MARKER_VALUE:
        raise LeaseFailure("fixture_marker_invalid")


def lease_path(root: Path) -> Path:
    return root / LEASE_DIR / LEASE_FILE


def _valid_transaction_id(value: Any) -> bool:
    return isinstance(value, str) and bool(ID_RE.fullmatch(value))


def _valid_owner(value: Any) -> bool:
    return isinstance(value, str) and bool(OWNER_RE.fullmatch(value))


def _payload(transaction_id: str, owner: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "lease_version": LEASE_VERSION,
        "transaction_id": transaction_id,
        "owner": owner,
        "state": "ACTIVE",
        "fixture_only": True,
        "canonical_write_performed": False,
    }


def acquire_lease(
    root: Path,
    transaction_id: str,
    owner: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    root = root.resolve()
    try:
        require_fixture(root)
    except LeaseFailure as exc:
        return None, [str(exc)]
    if not _valid_transaction_id(transaction_id):
        return None, ["transaction_id_invalid"]
    if not _valid_owner(owner):
        return None, ["owner_invalid"]

    path = lease_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _payload(transaction_id, owner)
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = read_json(path)
        if existing == payload:
            return existing, []
        if not isinstance(existing, dict):
            return None, ["lease_corrupt"]
        return None, ["lease_conflict"]
    except OSError:
        return None, ["lease_create_failed"]

    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass
        return None, ["lease_write_failed"]

    stored = read_json(path)
    if stored != payload:
        return None, ["lease_verify_failed"]
    return payload, []


def _release_path(path: Path) -> list[str]:
    try:
        path.unlink()
    except OSError:
        return ["lease_release_failed"]
    if path.exists():
        return ["lease_release_verify_failed"]
    return []


def _journal_release_state(root: Path, transaction_id: str) -> list[str]:
    """Return [] when a present journal proves the transaction is finalized."""

    path = journal_dir(root, transaction_id) / "journal.json"
    if not path.exists():
        # Pre-write failures are allowed to release without ever creating a journal.
        return []
    journal = read_json(path)
    if not isinstance(journal, dict):
        return ["recovery_journal_corrupt"]
    if journal.get("schema_version") != SCHEMA_VERSION:
        return ["recovery_schema"]
    if journal.get("recovery_version") != RECOVERY_VERSION:
        return ["recovery_version"]
    if journal.get("transaction_id") != transaction_id:
        return ["recovery_transaction_id"]
    if journal.get("fixture_only") is not True or journal.get("canonical_write_performed") is not False:
        return ["recovery_safety_flags"]

    state = journal.get("state")
    if state == "COMMITTED" and journal.get("receipt_verified") is True:
        return []
    if state == "RECOVERED" and journal.get("rollback_verified") is True:
        return []
    return ["transaction_not_finalized"]


def release_lease(
    root: Path,
    transaction_id: str,
    owner: str,
) -> list[str]:
    root = root.resolve()
    try:
        require_fixture(root)
    except LeaseFailure as exc:
        return [str(exc)]
    path = lease_path(root)
    existing = read_json(path)
    if not isinstance(existing, dict):
        return ["lease_missing"]
    if existing.get("lease_version") != LEASE_VERSION:
        return ["lease_version_mismatch"]
    if existing.get("transaction_id") != transaction_id:
        return ["lease_transaction_mismatch"]
    if existing.get("owner") != owner:
        return ["lease_owner_mismatch"]
    if existing.get("state") != "ACTIVE" or existing.get("fixture_only") is not True:
        return ["lease_state_invalid"]
    journal_errors = _journal_release_state(root, transaction_id)
    if journal_errors:
        return journal_errors
    return _release_path(path)


def release_recovered_lease(
    root: Path,
    transaction_id: str,
) -> list[str]:
    """Release a stale owner only after the exact transaction was fully recovered."""

    root = root.resolve()
    try:
        require_fixture(root)
    except LeaseFailure as exc:
        return [str(exc)]
    if not _valid_transaction_id(transaction_id):
        return ["transaction_id_invalid"]

    path = lease_path(root)
    existing = read_json(path)
    if not isinstance(existing, dict):
        return ["lease_missing"]
    if existing.get("lease_version") != LEASE_VERSION:
        return ["lease_version_mismatch"]
    if existing.get("transaction_id") != transaction_id:
        return ["lease_transaction_mismatch"]
    if existing.get("state") != "ACTIVE" or existing.get("fixture_only") is not True:
        return ["lease_state_invalid"]

    journal = read_json(journal_dir(root, transaction_id) / "journal.json")
    if not isinstance(journal, dict):
        return ["recovery_journal_missing"]
    if journal.get("schema_version") != SCHEMA_VERSION:
        return ["recovery_schema"]
    if journal.get("recovery_version") != RECOVERY_VERSION:
        return ["recovery_version"]
    if journal.get("transaction_id") != transaction_id:
        return ["recovery_transaction_id"]
    if journal.get("state") != "RECOVERED":
        return ["recovery_not_complete"]
    if journal.get("rollback_verified") is not True:
        return ["recovery_not_verified"]
    if journal.get("fixture_only") is not True or journal.get("canonical_write_performed") is not False:
        return ["recovery_safety_flags"]

    return _release_path(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Acquire/release an exclusive transaction lease in a local fixture only.")
    parser.add_argument("--root-dir", type=Path, required=True)
    parser.add_argument("--transaction-id", required=True)
    parser.add_argument("--owner")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--acquire", action="store_true")
    mode.add_argument("--release", action="store_true")
    mode.add_argument("--release-recovered", action="store_true")
    args = parser.parse_args()

    if args.acquire:
        if not isinstance(args.owner, str):
            print("candidate_transaction_lease_fixture_error action=acquire codes=owner_required canonical_write=0")
            return 2
        lease, errors = acquire_lease(args.root_dir, args.transaction_id, args.owner)
        if errors or lease is None:
            print(
                "candidate_transaction_lease_fixture_error "
                f"action=acquire codes={','.join(errors or ['failed'])} canonical_write=0"
            )
            return 2
        print(
            "candidate_transaction_lease_fixture_ok "
            f"action=acquire transaction_id={lease['transaction_id']} owner={lease['owner']} canonical_write=0"
        )
        return 0

    if args.release_recovered:
        errors = release_recovered_lease(args.root_dir, args.transaction_id)
        action = "release_recovered"
        owner_text = "recovery"
    else:
        if not isinstance(args.owner, str):
            print("candidate_transaction_lease_fixture_error action=release codes=owner_required canonical_write=0")
            return 2
        errors = release_lease(args.root_dir, args.transaction_id, args.owner)
        action = "release"
        owner_text = args.owner
    if errors:
        print(
            "candidate_transaction_lease_fixture_error "
            f"action={action} codes={','.join(errors)} canonical_write=0"
        )
        return 2
    print(
        "candidate_transaction_lease_fixture_ok "
        f"action={action} transaction_id={args.transaction_id} owner={owner_text} canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
