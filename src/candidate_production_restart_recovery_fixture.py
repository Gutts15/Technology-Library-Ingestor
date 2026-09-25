#!/usr/bin/env python3
"""Fresh-process production-shape restart recovery fixture over real rclone transport.

All physical remote writes are remapped below a private fixture root while the logical
journal/snapshot/canonical paths retain their production shapes. A temporary local bare
Git repository exercises the real production coordinator state machine.

The fixture proves:
- unresolved ACTIVE ownership blocks a competing transaction;
- UPDATE bytes are restored from SHA-bound snapshots;
- CREATE targets are removed;
- pre-existing MASTER/domain indexes are restored;
- indexes created by the interrupted transaction are removed;
- a fresh Python process persists a verified RECOVERED journal;
- coordinator release is allowed only after that recovered journal;
- a new transaction can acquire only after recovery release.

The real 00_LIBRARY tree and the live GitHub production coordinator are never written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_production_coordinator import (
    acquire,
    mark_canonical_write_started,
    read_state,
    release_prewrite_abort,
    release_recovered,
)
from candidate_production_recovery_engine import execute_recovery
from candidate_remote_recovery_probe import (
    join_remote,
    remote_cat,
    remote_copy_bytes,
    run_rclone,
)

SCHEMA_VERSION = 1
FIXTURE_VERSION = "0.1.0"
FIXTURE_ROOT = "99_INBOX/CANDIDATES/PRODUCTION_RESTART_RECOVERY_FIXTURE"

UPDATE_TARGET = "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-existing.md"
CREATE_TARGET = "00_LIBRARY/07_DESIGN_CREATIVE/TOOLS/technology-created.md"
MASTER = "00_LIBRARY/MASTER_INDEX.md"
AI_INDEX = "00_LIBRARY/04_AI_AGENTS/INDEX.md"
DESIGN_INDEX = "00_LIBRARY/07_DESIGN_CREATIVE/INDEX.md"

UPDATE_BEFORE = b"existing-before\n"
UPDATE_AFTER = b"existing-after\n"
CREATE_AFTER = b"created-during-transaction\n"
MASTER_BEFORE = b"# MASTER_INDEX\nold\n"
MASTER_AFTER = b"# MASTER_INDEX\nnew\n"
AI_INDEX_BEFORE = b"# INDEX AI\nold\n"
AI_INDEX_AFTER = b"# INDEX AI\nnew\n"
DESIGN_INDEX_AFTER = b"# INDEX DESIGN\ncreated\n"


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


def remote_write(remote: str, fixture_id: str, logical: str, raw: bytes) -> bool:
    try:
        rel = physical_rel(fixture_id, logical)
    except ValueError:
        return False
    return remote_copy_bytes(remote, rel, raw)


def remote_read(remote: str, fixture_id: str, logical: str) -> bytes | None:
    try:
        rel = physical_rel(fixture_id, logical)
    except ValueError:
        return None
    return remote_cat(remote, rel)


def remote_delete(remote: str, fixture_id: str, logical: str) -> bool:
    try:
        rel = physical_rel(fixture_id, logical)
    except ValueError:
        return False
    result = run_rclone(["deletefile", join_remote(remote, rel), "--log-level", "ERROR"])
    if result.returncode != 0 and remote_cat(remote, rel) is not None:
        return False
    return remote_cat(remote, rel) is None


def snapshot_logical_path(transaction_id: str, owner: str, name: str) -> str:
    return str(
        PurePosixPath("99_INBOX/CANDIDATES/PRODUCTION_RECOVERY")
        / transaction_id
        / owner
        / "snapshots"
        / name
    )


def journal_logical_path(transaction_id: str, owner: str) -> str:
    return str(
        PurePosixPath("99_INBOX/CANDIDATES/PRODUCTION_RECOVERY")
        / transaction_id
        / owner
        / "journal.json"
    )


def build_recovery_journal(
    *,
    transaction_id: str,
    batch_id: str,
    owner: str,
) -> dict[str, Any]:
    update_snap = snapshot_logical_path(transaction_id, owner, "update.bin")
    master_snap = snapshot_logical_path(transaction_id, owner, "master.bin")
    ai_snap = snapshot_logical_path(transaction_id, owner, "ai-index.bin")
    journal_path = journal_logical_path(transaction_id, owner)
    return {
        "schema_version": SCHEMA_VERSION,
        "prewrite_arm_version": "0.1.0",
        "state": "RECOVERY_REQUIRED",
        "transaction_id": transaction_id,
        "batch_id": batch_id,
        "owner": owner,
        "journal_path": journal_path,
        "master_index_before_sha256": sha256(MASTER_BEFORE),
        "live_preconditions_verified_under_ownership": True,
        "snapshots_verified": True,
        "safe_abort_verified": False,
        "coordinator_release_allowed": False,
        "canonical_write_performed": True,
        "items": [
            {
                "action": "UPDATE",
                "record_id": "a" * 20,
                "target_path": UPDATE_TARGET,
                "existed_before": True,
                "before_sha256": sha256(UPDATE_BEFORE),
                "snapshot_path": update_snap,
            },
            {
                "action": "CREATE",
                "record_id": "b" * 20,
                "target_path": CREATE_TARGET,
                "existed_before": False,
                "before_sha256": None,
                "snapshot_path": None,
            },
        ],
        "indexes": [
            {
                "path": MASTER,
                "existed_before": True,
                "before_sha256": sha256(MASTER_BEFORE),
                "snapshot_path": master_snap,
            },
            {
                "path": AI_INDEX,
                "existed_before": True,
                "before_sha256": sha256(AI_INDEX_BEFORE),
                "snapshot_path": ai_snap,
            },
            {
                "path": DESIGN_INDEX,
                "existed_before": False,
                "before_sha256": None,
                "snapshot_path": None,
            },
        ],
    }


def persist_fixture_state(
    remote: str,
    fixture_id: str,
    journal: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    snapshot_bytes = {
        journal["items"][0]["snapshot_path"]: UPDATE_BEFORE,
        journal["indexes"][0]["snapshot_path"]: MASTER_BEFORE,
        journal["indexes"][1]["snapshot_path"]: AI_INDEX_BEFORE,
    }
    for logical, raw in snapshot_bytes.items():
        if not remote_write(remote, fixture_id, logical, raw):
            errors.append(f"snapshot_write_failed:{logical}")
        elif remote_read(remote, fixture_id, logical) != raw:
            errors.append(f"snapshot_verify_failed:{logical}")

    canonical = {
        UPDATE_TARGET: UPDATE_AFTER,
        CREATE_TARGET: CREATE_AFTER,
        MASTER: MASTER_AFTER,
        AI_INDEX: AI_INDEX_AFTER,
        DESIGN_INDEX: DESIGN_INDEX_AFTER,
    }
    for logical, raw in canonical.items():
        if not remote_write(remote, fixture_id, logical, raw):
            errors.append(f"mutated_write_failed:{logical}")
        elif remote_read(remote, fixture_id, logical) != raw:
            errors.append(f"mutated_verify_failed:{logical}")

    journal_raw = (json.dumps(journal, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if not remote_write(remote, fixture_id, journal["journal_path"], journal_raw):
        errors.append("journal_write_failed")
    elif remote_read(remote, fixture_id, journal["journal_path"]) != journal_raw:
        errors.append("journal_verify_failed")
    return sorted(set(errors))


def resume_recovery(
    remote: str,
    git_remote_url: str,
    fixture_id: str,
    transaction_id: str,
    batch_id: str,
    owner: str,
    result_path: Path | None,
) -> int:
    if not safe_fixture_id(fixture_id):
        print("candidate_production_restart_recovery_fixture_error mode=resume code=unsafe_fixture_id canonical_write=0")
        return 2

    jpath = journal_logical_path(transaction_id, owner)
    raw = remote_read(remote, fixture_id, jpath)
    if raw is None:
        print("candidate_production_restart_recovery_fixture_error mode=resume code=journal_missing canonical_write=0")
        return 2
    try:
        journal = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        print("candidate_production_restart_recovery_fixture_error mode=resume code=journal_invalid canonical_write=0")
        return 2
    if not isinstance(journal, dict):
        print("candidate_production_restart_recovery_fixture_error mode=resume code=journal_invalid canonical_write=0")
        return 2
    if (
        journal.get("transaction_id") != transaction_id
        or journal.get("batch_id") != batch_id
        or journal.get("owner") != owner
        or journal.get("journal_path") != jpath
    ):
        print("candidate_production_restart_recovery_fixture_error mode=resume code=journal_identity_mismatch canonical_write=0")
        return 2

    _, active, coord_errors = read_state(git_remote_url)
    if (
        coord_errors
        or not isinstance(active, dict)
        or active.get("state") != "ACTIVE"
        or active.get("transaction_id") != transaction_id
        or active.get("batch_id") != batch_id
        or active.get("owner") != owner
        or active.get("journal_path") != jpath
        or active.get("canonical_write_performed") is not True
    ):
        print("candidate_production_restart_recovery_fixture_error mode=resume code=coordinator_active_state_invalid canonical_write=0")
        return 2

    recovered, errors = execute_recovery(
        journal,
        read_canonical=lambda path: remote_read(remote, fixture_id, path),
        read_snapshot=lambda path: remote_read(remote, fixture_id, path),
        write_canonical=lambda path, data: remote_write(remote, fixture_id, path, data),
        delete_canonical=lambda path: remote_delete(remote, fixture_id, path),
    )
    if errors or recovered is None:
        print(
            "candidate_production_restart_recovery_fixture_error mode=resume "
            f"codes={','.join(errors or ['recovery_failed'])} canonical_write=0"
        )
        return 2

    recovered_raw = (json.dumps(recovered, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if not remote_write(remote, fixture_id, jpath, recovered_raw):
        print("candidate_production_restart_recovery_fixture_error mode=resume code=recovered_journal_write_failed canonical_write=0")
        return 2
    if remote_read(remote, fixture_id, jpath) != recovered_raw:
        print("candidate_production_restart_recovery_fixture_error mode=resume code=recovered_journal_readback_failed canonical_write=0")
        return 2

    release_errors = release_recovered(
        git_remote_url,
        transaction_id=transaction_id,
        batch_id=batch_id,
        owner=owner,
        journal_path=jpath,
        recovered_journal_sha256=sha256(recovered_raw),
    )
    if release_errors:
        print(
            "candidate_production_restart_recovery_fixture_error mode=resume "
            f"codes={','.join(release_errors)} canonical_write=0"
        )
        return 2

    _, free, free_errors = read_state(git_remote_url)
    if (
        free_errors
        or not isinstance(free, dict)
        or free.get("state") != "FREE"
        or free.get("release_reason") != "VERIFIED_RECOVERED"
        or free.get("released_recovered_journal_sha256") != sha256(recovered_raw)
    ):
        print("candidate_production_restart_recovery_fixture_error mode=resume code=recovery_release_verify_failed canonical_write=0")
        return 2

    result = {
        "state": "RECOVERED",
        "rollback_verified": recovered.get("rollback_verified") is True,
        "restart_recovery_verified": recovered.get("restart_recovery_verified") is True,
        "coordinator_released": True,
        "recovered_journal_sha256": sha256(recovered_raw),
        "canonical_write_performed": False,
    }
    if result_path is not None:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_production_restart_recovery_fixture_resume_ok state=RECOVERED "
        "rollback_verified=1 coordinator_released=1 canonical_write=0"
    )
    return 0


def apply_fixture(remote: str, out: Path | None) -> int:
    if not shutil.which("rclone") or not shutil.which("git"):
        print("candidate_production_restart_recovery_fixture_error code=dependency_missing canonical_write=0")
        return 2

    fixture_id = uuid.uuid4().hex[:24]
    tx = sha256((fixture_id + ":production-restart").encode("utf-8"))[:20]
    batch = sha256((fixture_id + ":batch").encode("utf-8"))[:20]
    owner = "candidate-publisher-" + fixture_id[:12]
    jpath = journal_logical_path(tx, owner)

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "fixture_version": FIXTURE_VERSION,
        "state": "FAILED",
        "fixture_id": fixture_id,
        "fixture_path": fixture_prefix(fixture_id),
        "pending_recovery_blocks_new_transaction": False,
        "fresh_process_recovery_verified": False,
        "rollback_verified": False,
        "recovery_release_verified": False,
        "new_transaction_after_recovery_verified": False,
        "cleanup_verified": False,
        "canonical_write_performed": False,
    }
    errors: list[str] = []

    try:
        journal = build_recovery_journal(
            transaction_id=tx,
            batch_id=batch,
            owner=owner,
        )
        errors.extend(persist_fixture_state(remote, fixture_id, journal))
        if errors:
            return report, sorted(set(errors))

        with tempfile.TemporaryDirectory(prefix="tl-production-restart-coord-") as temp:
            bare = Path(temp) / "coord.git"
            init = subprocess.run(
                ["git", "init", "--bare", str(bare)],
                check=False,
                capture_output=True,
                text=True,
            )
            if init.returncode != 0:
                return report, ["coordinator_fixture_init_failed"]
            git_remote = str(bare)

            _, active, acquire_errors = acquire(
                git_remote,
                transaction_id=tx,
                batch_id=batch,
                owner=owner,
                journal_path=jpath,
            )
            if acquire_errors or active is None:
                return report, acquire_errors or ["coordinator_acquire_failed"]

            boundary_errors = mark_canonical_write_started(
                git_remote,
                transaction_id=tx,
                batch_id=batch,
                owner=owner,
                journal_path=jpath,
            )
            if boundary_errors:
                return report, boundary_errors

            # A different transaction must be blocked while recovery remains unresolved.
            other_tx = "f" * 20
            other_batch = "e" * 20
            other_owner = "candidate-publisher-" + "d" * 12
            _, competing, competing_errors = acquire(
                git_remote,
                transaction_id=other_tx,
                batch_id=other_batch,
                owner=other_owner,
                journal_path=journal_logical_path(other_tx, other_owner),
            )
            report["pending_recovery_blocks_new_transaction"] = (
                competing is None and competing_errors == ["coordinator_busy"]
            )
            if not report["pending_recovery_blocks_new_transaction"]:
                return report, ["pending_recovery_did_not_block"]

            with tempfile.TemporaryDirectory(prefix="tl-production-restart-child-") as child_temp:
                result_path = Path(child_temp) / "resume-result.json"
                child = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--remote",
                        remote,
                        "--git-remote-url",
                        git_remote,
                        "--resume-recovery",
                        fixture_id,
                        "--transaction-id",
                        tx,
                        "--batch-id",
                        batch,
                        "--owner",
                        owner,
                        "--resume-result",
                        str(result_path),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=os.environ.copy(),
                )
                if child.returncode != 0:
                    return report, ["fresh_process_recovery_failed"]
                try:
                    child_result = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    child_result = None
                report["fresh_process_recovery_verified"] = (
                    isinstance(child_result, dict)
                    and child_result.get("state") == "RECOVERED"
                    and child_result.get("rollback_verified") is True
                    and child_result.get("coordinator_released") is True
                )
                if not report["fresh_process_recovery_verified"]:
                    return report, ["fresh_process_result_invalid"]

            report["rollback_verified"] = (
                remote_read(remote, fixture_id, UPDATE_TARGET) == UPDATE_BEFORE
                and remote_read(remote, fixture_id, CREATE_TARGET) is None
                and remote_read(remote, fixture_id, MASTER) == MASTER_BEFORE
                and remote_read(remote, fixture_id, AI_INDEX) == AI_INDEX_BEFORE
                and remote_read(remote, fixture_id, DESIGN_INDEX) is None
            )
            if not report["rollback_verified"]:
                return report, ["rollback_bytes_mismatch"]

            _, free, free_errors = read_state(git_remote)
            report["recovery_release_verified"] = (
                not free_errors
                and isinstance(free, dict)
                and free.get("state") == "FREE"
                and free.get("release_reason") == "VERIFIED_RECOVERED"
            )
            if not report["recovery_release_verified"]:
                return report, ["recovery_release_not_verified"]

            # After verified recovery release, a new transaction may acquire normally.
            new_tx = "1" * 20
            new_batch = "2" * 20
            new_owner = "candidate-publisher-" + "3" * 12
            new_journal = journal_logical_path(new_tx, new_owner)
            _, new_active, new_errors = acquire(
                git_remote,
                transaction_id=new_tx,
                batch_id=new_batch,
                owner=new_owner,
                journal_path=new_journal,
            )
            if new_errors or new_active is None:
                return report, new_errors or ["post_recovery_acquire_failed"]
            release_errors = release_prewrite_abort(
                git_remote,
                transaction_id=new_tx,
                batch_id=new_batch,
                owner=new_owner,
                journal_path=new_journal,
            )
            report["new_transaction_after_recovery_verified"] = not release_errors
            if release_errors:
                return report, release_errors

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
        description="Prove production-shape restart recovery only in private remapped fixture scope."
    )
    parser.add_argument("--remote", required=True)
    parser.add_argument("--apply-remote-fixture", action="store_true")
    parser.add_argument("--resume-recovery")
    parser.add_argument("--git-remote-url")
    parser.add_argument("--transaction-id")
    parser.add_argument("--batch-id")
    parser.add_argument("--owner")
    parser.add_argument("--resume-result", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if args.resume_recovery is not None:
        required = [args.git_remote_url, args.transaction_id, args.batch_id, args.owner]
        if any(not value for value in required):
            print("candidate_production_restart_recovery_fixture_error mode=resume code=resume_identity_missing canonical_write=0")
            return 2
        return resume_recovery(
            str(args.remote),
            str(args.git_remote_url),
            str(args.resume_recovery),
            str(args.transaction_id),
            str(args.batch_id),
            str(args.owner),
            args.resume_result,
        )

    if not args.apply_remote_fixture:
        print(
            "candidate_production_restart_recovery_fixture_ok mode=plan "
            f"fixture_root={FIXTURE_ROOT} remote_write=0 canonical_write=0"
        )
        return 0

    report, errors = apply_fixture(str(args.remote), args.out)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if errors or report.get("state") != "PASS":
        print(
            "candidate_production_restart_recovery_fixture_error "
            f"codes={','.join(errors or ['fixture_failed'])} "
            f"cleanup_verified={1 if report.get('cleanup_verified') else 0} canonical_write=0"
        )
        return 2

    print(
        "candidate_production_restart_recovery_fixture_ok mode=apply_fixture state=PASS "
        "pending_blocks=1 fresh_process=1 rollback_verified=1 recovered_release=1 "
        "post_recovery_acquire=1 cleanup_verified=1 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
