#!/usr/bin/env python3
"""Prove receipt persistence + COMMITTED journal + coordinator release in private scope.

This fixture uses the real rclone transport for durable receipt/journal bytes, but all
physical remote writes are confined below:

  99_INBOX/CANDIDATES/COMMIT_REMOTE_FIXTURE/<fixture_id>/

The Git coordinator uses a temporary local bare repository so the production
coordinator state machine is exercised without touching the live production ref.

No physical write can target the real 00_LIBRARY tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_production_coordinator import (
    acquire,
    mark_canonical_write_started,
    read_state,
    release_committed,
)
from candidate_remote_recovery_probe import (
    join_remote,
    remote_cat,
    remote_copy_bytes,
    run_rclone,
)
from candidate_transaction_commit_engine import build_committed_journal
from candidate_transaction_receipt import build_receipt

SCHEMA_VERSION = 1
FIXTURE_VERSION = "0.1.0"
FIXTURE_ROOT = "99_INBOX/CANDIDATES/COMMIT_REMOTE_FIXTURE"

TARGET = "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-commit-fixture.md"
CONTENT = "99_INBOX/CANDIDATES/PUBLISH_READY/records/commit-fixture.md"
MASTER = "00_LIBRARY/MASTER_INDEX.md"

TARGET_BYTES = (
    b"RECORD_ID: aaaaaaaaaaaaaaaaaaaa\n"
    b"TYPE: TECHNOLOGY\n"
    b"STATUS: REFERENCE\n"
    b"DOMAIN: 04_AI_AGENTS\n"
    b"CATEGORY: TOOLS\n"
    b"TITLE: Commit Fixture\n"
    b"\n# SUMMARY\nFixture.\n"
)
MASTER_BEFORE = b"# MASTER_INDEX\n\nGENERATED: TRUE\nRECORDS: 0\n"
MASTER_AFTER = (
    b"# MASTER_INDEX\n\nGENERATED: TRUE\nRECORDS: 1\n"
    b"\n## 04_AI_AGENTS\n\n### TOOLS\n\n"
    b"- TECHNOLOGY - Commit Fixture [REFERENCE] "
    b"(`00_LIBRARY/04_AI_AGENTS/TOOLS/technology-commit-fixture.md`)\n"
)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def safe_fixture_id(value: str) -> bool:
    return len(value) == 24 and all(ch in "0123456789abcdef" for ch in value)


def fixture_prefix(fixture_id: str) -> str:
    if not safe_fixture_id(fixture_id):
        raise ValueError("unsafe_fixture_id")
    return str(PurePosixPath(FIXTURE_ROOT) / fixture_id)


def physical_rel(fixture_id: str, logical_path: str) -> str:
    prefix = fixture_prefix(fixture_id)
    path = PurePosixPath(logical_path.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("unsafe_logical_path")
    normalized = path.as_posix().strip("/")
    if not (
        normalized.startswith("00_LIBRARY/")
        or normalized.startswith("99_INBOX/CANDIDATES/")
    ):
        raise ValueError("logical_path_outside_fixture_contract")
    return str(PurePosixPath(prefix) / normalized)


def cleanup_fixture(remote: str, fixture_id: str) -> bool:
    prefix = fixture_prefix(fixture_id)
    purge = run_rclone(["purge", join_remote(remote, prefix), "--log-level", "ERROR"])
    if purge.returncode != 0:
        return False
    check = run_rclone(["lsjson", join_remote(remote, prefix), "--log-level", "ERROR"])
    if check.returncode != 0:
        return True
    try:
        rows = json.loads(check.stdout or "[]")
    except json.JSONDecodeError:
        return False
    return rows == []


def build_plan() -> dict[str, Any]:
    tx_seed = json.dumps(
        {
            "master": sha256(MASTER_BEFORE),
            "target": TARGET,
            "content": sha256(TARGET_BYTES),
        },
        sort_keys=True,
    ).encode("utf-8")
    return {
        "schema_version": SCHEMA_VERSION,
        "transaction_plan_version": "0.2.0",
        "transaction_id": sha256(tx_seed)[:20],
        "master_index_sha256": sha256(MASTER_BEFORE),
        "canonical_write_performed": False,
        "requires_live_revalidation": True,
        "requires_atomic_record_writes": True,
        "requires_index_rebuild_after_write": True,
        "requires_byte_verification_after_write": True,
        "requires_rollback_on_partial_failure": True,
        "items": [
            {
                "lane": "NEW",
                "action": "CREATE",
                "package_id": "b" * 20,
                "revision_key": "c" * 20,
                "record_id": "a" * 20,
                "target_path": TARGET,
                "content_path": CONTENT,
                "content_sha256": sha256(TARGET_BYTES),
                "precondition": "TARGET_ABSENT",
            }
        ],
        "counts": {"create": 1, "update": 0, "unchanged": 0, "writes": 1},
    }


def build_commit_ready_journal(
    *,
    transaction_id: str,
    batch_id: str,
    owner: str,
    journal_path: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "fixture_version": FIXTURE_VERSION,
        "fixture_only": True,
        "state": "COMMITTED_PENDING_RECEIPT",
        "transaction_id": transaction_id,
        "batch_id": batch_id,
        "owner": owner,
        "journal_path": journal_path,
        "canonical_write_performed": True,
        "snapshots_verified": True,
        "live_preconditions_verified_under_ownership": True,
        "coordinator_release_allowed": False,
    }


def run_fixture(remote: str) -> tuple[dict[str, Any], list[str]]:
    if not shutil.which("rclone"):
        return {"state": "FAILED", "canonical_write_performed": False}, ["rclone_missing"]
    if not shutil.which("git"):
        return {"state": "FAILED", "canonical_write_performed": False}, ["git_missing"]

    fixture_id = uuid.uuid4().hex[:24]
    prefix = fixture_prefix(fixture_id)
    plan = build_plan()
    tx = str(plan["transaction_id"])
    batch = "d" * 20
    owner = "candidate-publisher-" + uuid.uuid4().hex[:12]
    journal_rel = str(PurePosixPath(prefix) / "journal.json")
    receipt_rel = str(PurePosixPath(prefix) / "receipt.json")

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "fixture_version": FIXTURE_VERSION,
        "state": "FAILED",
        "fixture_id": fixture_id,
        "fixture_path": prefix,
        "receipt_verified": False,
        "receipt_persisted": False,
        "journal_committed": False,
        "coordinator_released": False,
        "cleanup_verified": False,
        "canonical_write_performed": False,
    }
    errors: list[str] = []

    try:
        # Seed only remapped private fixture bytes.
        seeds = {
            CONTENT: TARGET_BYTES,
            TARGET: TARGET_BYTES,
            MASTER: MASTER_AFTER,
        }
        for logical, raw in seeds.items():
            rel = physical_rel(fixture_id, logical)
            if not remote_copy_bytes(remote, rel, raw):
                errors.append(f"seed_write_failed:{logical}")
            elif remote_cat(remote, rel) != raw:
                errors.append(f"seed_verify_failed:{logical}")
        if errors:
            return report, sorted(set(errors))

        def reader(logical: str) -> bytes | None:
            try:
                rel = physical_rel(fixture_id, logical)
            except ValueError:
                return None
            return remote_cat(remote, rel)

        receipt, receipt_errors = build_receipt(
            plan,
            MASTER_BEFORE,
            MASTER_AFTER,
            reader,
        )
        if receipt_errors or receipt is None:
            errors.extend(receipt_errors or ["receipt_build_failed"])
            return report, sorted(set(errors))
        report["receipt_verified"] = True

        receipt_raw = (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if not remote_copy_bytes(remote, receipt_rel, receipt_raw):
            errors.append("receipt_write_failed")
        elif remote_cat(remote, receipt_rel) != receipt_raw:
            errors.append("receipt_readback_failed")
        else:
            report["receipt_persisted"] = True
        if errors:
            return report, sorted(set(errors))

        with tempfile.TemporaryDirectory(prefix="tl-commit-coordinator-fixture-") as temp:
            bare = Path(temp) / "coord.git"
            init = subprocess.run(
                ["git", "init", "--bare", str(bare)],
                check=False,
                capture_output=True,
                text=True,
            )
            if init.returncode != 0:
                errors.append("coordinator_fixture_init_failed")
                return report, sorted(set(errors))
            git_remote = str(bare)

            _, active, acquire_errors = acquire(
                git_remote,
                transaction_id=tx,
                batch_id=batch,
                owner=owner,
                journal_path=journal_rel,
            )
            if acquire_errors or active is None:
                errors.extend(acquire_errors or ["coordinator_acquire_failed"])
                return report, sorted(set(errors))

            write_errors = mark_canonical_write_started(
                git_remote,
                transaction_id=tx,
                batch_id=batch,
                owner=owner,
                journal_path=journal_rel,
            )
            if write_errors:
                errors.extend(write_errors)
                return report, sorted(set(errors))

            pending = build_commit_ready_journal(
                transaction_id=tx,
                batch_id=batch,
                owner=owner,
                journal_path=journal_rel,
            )
            pending_raw = (json.dumps(pending, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if not remote_copy_bytes(remote, journal_rel, pending_raw):
                errors.append("pending_journal_write_failed")
                return report, sorted(set(errors))
            if remote_cat(remote, journal_rel) != pending_raw:
                errors.append("pending_journal_readback_failed")
                return report, sorted(set(errors))

            terminal, commit_errors = build_committed_journal(pending, receipt, receipt_raw)
            if commit_errors or terminal is None:
                errors.extend(commit_errors or ["commit_journal_build_failed"])
                return report, sorted(set(errors))

            terminal_raw = (json.dumps(terminal, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if not remote_copy_bytes(remote, journal_rel, terminal_raw):
                errors.append("committed_journal_write_failed")
                return report, sorted(set(errors))
            if remote_cat(remote, journal_rel) != terminal_raw:
                errors.append("committed_journal_readback_failed")
                return report, sorted(set(errors))
            report["journal_committed"] = True

            release_errors = release_committed(
                git_remote,
                transaction_id=tx,
                batch_id=batch,
                owner=owner,
                journal_path=journal_rel,
                receipt_sha256=sha256(receipt_raw),
            )
            if release_errors:
                errors.extend(release_errors)
                return report, sorted(set(errors))

            _, free, read_errors = read_state(git_remote)
            if (
                read_errors
                or not isinstance(free, dict)
                or free.get("state") != "FREE"
                or free.get("release_reason") != "VERIFIED_COMMITTED"
                or free.get("released_receipt_sha256") != sha256(receipt_raw)
            ):
                errors.extend(read_errors or ["coordinator_release_readback_failed"])
                return report, sorted(set(errors))
            report["coordinator_released"] = True

        report["state"] = "PASS"
    finally:
        cleaned = cleanup_fixture(remote, fixture_id)
        report["cleanup_verified"] = cleaned
        if not cleaned:
            errors.append("cleanup_failed")
        if errors:
            report["state"] = "FAILED"

    return report, sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prove receipt/journal commit flow over private rclone fixture state."
    )
    parser.add_argument("--remote", required=True)
    parser.add_argument("--apply-remote-fixture", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if not args.apply_remote_fixture:
        print(
            "candidate_transaction_commit_remote_fixture_ok mode=plan "
            f"fixture_root={FIXTURE_ROOT} remote_write=0 canonical_write=0"
        )
        return 0

    report, errors = run_fixture(str(args.remote))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if errors or report.get("state") != "PASS":
        print(
            "candidate_transaction_commit_remote_fixture_error "
            f"codes={','.join(errors or ['fixture_failed'])} "
            f"cleanup_verified={1 if report.get('cleanup_verified') else 0} canonical_write=0"
        )
        return 2

    print(
        "candidate_transaction_commit_remote_fixture_ok mode=apply_fixture state=PASS "
        "receipt_verified=1 receipt_persisted=1 journal_committed=1 "
        "coordinator_released=1 cleanup_verified=1 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
