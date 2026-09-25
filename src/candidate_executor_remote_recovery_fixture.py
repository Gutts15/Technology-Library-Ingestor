#!/usr/bin/env python3
"""Exercise hard-interruption-style restart recovery for the candidate executor wiring.

This fixture composes the already-proven remote executor success-path wiring with a
fresh Python recovery process. It writes only synthetic bytes below
``99_INBOX/CANDIDATES/EXECUTOR_REMOTE_FIXTURE`` and a disposable
``tl-coordination-fixture-executor-*`` Git ref.

Parent path:
  seed fixture -> acquire exact-ref coordinator -> revalidate -> persist recovery
  snapshot/journal -> mark WRITE_READY_FIXTURE -> mutate synthetic UPDATE/CREATE
  targets -> disappear from the recovery logic -> spawn a fresh Python process.

Fresh-process path:
  reopen coordinator + journal + snapshot -> prove the same durable transaction is
  still ACTIVE -> restore prior bytes -> remove transaction-created target -> verify
  rollback exactly -> mark journal RECOVERED -> release coordinator only after the
  verified rollback.

The fixture never reads or writes ``00_LIBRARY``, never marks candidate settlement
eligible and never authorizes production publication.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from candidate_executor_remote_wiring_fixture import (
    AFTER_BYTES,
    BEFORE_BYTES,
    CREATED_BYTES,
    CREATE_NAME,
    EXISTING_NAME,
    FIXTURE_ROOT,
    JOURNAL_NAME,
    SNAPSHOT_NAME,
    build_prepared_journal,
    cleanup_remote_prefix,
    executor_fixture_ref,
    fixture_prefix,
    fixture_rel,
    prepare_owned_acquire,
    prepare_owned_release,
    sha256,
    validate_journal,
)
from candidate_git_ref_remote_probe import cleanup_ref, ls_remote, push_expected, read_ref_state, seed_ref
from candidate_remote_recovery_probe import (
    join_remote,
    remote_cat,
    remote_copy_bytes,
    remote_copy_json,
    remote_json,
    run_rclone,
)

SCHEMA_VERSION = 1
RECOVERY_FIXTURE_VERSION = "0.1.0"
RESULT_NAME = "executor-recovery-result.json"


class RecoveryFixtureFailure(RuntimeError):
    pass


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise RecoveryFixtureFailure(code)


def build_write_ready_journal(prepared: dict[str, Any]) -> dict[str, Any]:
    payload = dict(prepared)
    payload["state"] = "WRITE_READY_FIXTURE"
    payload["fixture_write_ready"] = True
    return payload


def build_recovered_journal(write_ready: dict[str, Any]) -> dict[str, Any]:
    payload = dict(write_ready)
    payload["state"] = "RECOVERED"
    payload["fixture_write_ready"] = False
    payload["rollback_verified"] = True
    payload["restart_recovery_verified"] = True
    payload["restored_existing_sha256"] = sha256(BEFORE_BYTES)
    payload["removed_created_target_verified"] = True
    payload["receipt_verified"] = False
    payload["production_publish_authorized"] = False
    payload["canonical_write_performed"] = False
    return payload


def safe_resume_identity(
    fixture_id: str,
    transaction_id: str,
    owner: str,
    coordinator_ref: str,
) -> bool:
    try:
        expected_ref = executor_fixture_ref(fixture_id)
        fixture_prefix(fixture_id)
    except ValueError:
        return False
    return (
        len(transaction_id) == 20
        and all(ch in "0123456789abcdef" for ch in transaction_id)
        and owner == "executor-recovery-" + fixture_id[:12]
        and coordinator_ref == expected_ref
    )


def _delete_remote_file(remote_root: str, relative: str) -> bool:
    result = run_rclone(["deletefile", join_remote(remote_root, relative), "--log-level", "ERROR"])
    if result.returncode != 0:
        return False
    return remote_cat(remote_root, relative) is None


def resume_recovery(
    remote_root: str,
    git_remote_url: str,
    fixture_id: str,
    transaction_id: str,
    owner: str,
    coordinator_ref: str,
    result_path: Path | None,
) -> int:
    if not safe_resume_identity(fixture_id, transaction_id, owner, coordinator_ref):
        print("candidate_executor_remote_recovery_fixture_error mode=resume code=unsafe_resume_identity authorized=0 canonical_write=0")
        return 2
    if not shutil.which("git") or not shutil.which("rclone"):
        print("candidate_executor_remote_recovery_fixture_error mode=resume code=dependency_missing authorized=0 canonical_write=0")
        return 2

    existing_rel = fixture_rel(fixture_id, EXISTING_NAME)
    created_rel = fixture_rel(fixture_id, CREATE_NAME)
    snapshot_rel = fixture_rel(fixture_id, SNAPSHOT_NAME)
    journal_rel = fixture_rel(fixture_id, JOURNAL_NAME)

    errors: list[str] = []
    active_sha, active_state, active_errors = read_ref_state(git_remote_url, coordinator_ref)
    if active_errors or not isinstance(active_sha, str) or not active_sha:
        errors.append("coordinator_restart_read_failed")
    elif not isinstance(active_state, dict):
        errors.append("coordinator_restart_state_missing")
    else:
        if active_state.get("state") != "ACTIVE":
            errors.append("coordinator_restart_not_active")
        if active_state.get("transaction_id") != transaction_id:
            errors.append("coordinator_restart_transaction_mismatch")
        if active_state.get("owner") != owner:
            errors.append("coordinator_restart_owner_mismatch")

    journal = remote_json(remote_root, journal_rel)
    journal_errors = validate_journal(
        journal,
        fixture_id,
        transaction_id,
        owner,
        coordinator_ref,
        expected_state="WRITE_READY_FIXTURE",
    )
    errors.extend(journal_errors)
    if isinstance(journal, dict) and journal.get("fixture_write_ready") is not True:
        errors.append("journal_not_write_ready")

    snapshot = remote_cat(remote_root, snapshot_rel)
    existing = remote_cat(remote_root, existing_rel)
    created = remote_cat(remote_root, created_rel)
    if snapshot != BEFORE_BYTES:
        errors.append("snapshot_restart_verify_failed")
    if existing != AFTER_BYTES:
        errors.append("mutated_existing_not_observed")
    if created != CREATED_BYTES:
        errors.append("created_target_not_observed")

    if errors:
        print(
            "candidate_executor_remote_recovery_fixture_error mode=resume "
            f"codes={','.join(sorted(set(errors)))} authorized=0 canonical_write=0"
        )
        return 2

    if not remote_copy_bytes(remote_root, existing_rel, snapshot or b""):
        print("candidate_executor_remote_recovery_fixture_error mode=resume code=restore_existing_failed authorized=0 canonical_write=0")
        return 2
    if remote_cat(remote_root, existing_rel) != BEFORE_BYTES:
        print("candidate_executor_remote_recovery_fixture_error mode=resume code=restore_existing_verify_failed authorized=0 canonical_write=0")
        return 2
    if not _delete_remote_file(remote_root, created_rel):
        print("candidate_executor_remote_recovery_fixture_error mode=resume code=remove_created_failed authorized=0 canonical_write=0")
        return 2

    recovered = build_recovered_journal(journal or {})
    if not remote_copy_json(remote_root, journal_rel, recovered):
        print("candidate_executor_remote_recovery_fixture_error mode=resume code=recovered_journal_write_failed authorized=0 canonical_write=0")
        return 2
    stored_recovered = remote_json(remote_root, journal_rel)
    recovered_errors = validate_journal(
        stored_recovered,
        fixture_id,
        transaction_id,
        owner,
        coordinator_ref,
        expected_state="RECOVERED",
    )
    if (
        recovered_errors
        or stored_recovered != recovered
        or not isinstance(stored_recovered, dict)
        or stored_recovered.get("rollback_verified") is not True
        or stored_recovered.get("restart_recovery_verified") is not True
        or stored_recovered.get("removed_created_target_verified") is not True
    ):
        print("candidate_executor_remote_recovery_fixture_error mode=resume code=recovered_journal_verify_failed authorized=0 canonical_write=0")
        return 2

    release_base_sha, release_state, release_errors = read_ref_state(git_remote_url, coordinator_ref)
    if release_errors or not isinstance(release_base_sha, str) or not release_base_sha or not isinstance(release_state, dict):
        print("candidate_executor_remote_recovery_fixture_error mode=resume code=coordinator_release_read_failed authorized=0 canonical_write=0")
        return 2
    if (
        release_state.get("state") != "ACTIVE"
        or release_state.get("transaction_id") != transaction_id
        or release_state.get("owner") != owner
    ):
        print("candidate_executor_remote_recovery_fixture_error mode=resume code=coordinator_release_identity_failed authorized=0 canonical_write=0")
        return 2

    release_work, _, prep_errors = prepare_owned_release(
        git_remote_url,
        coordinator_ref,
        release_base_sha,
        transaction_id=transaction_id,
        owner=owner,
        generation=int(release_state.get("generation") or 0) + 1,
    )
    if prep_errors or release_work is None:
        print("candidate_executor_remote_recovery_fixture_error mode=resume code=coordinator_release_prepare_failed authorized=0 canonical_write=0")
        return 2
    try:
        released, _ = push_expected(release_work, coordinator_ref, release_base_sha)
    finally:
        shutil.rmtree(release_work, ignore_errors=True)
    if not released:
        print("candidate_executor_remote_recovery_fixture_error mode=resume code=coordinator_release_cas_failed authorized=0 canonical_write=0")
        return 2

    free_sha, free_state, free_errors = read_ref_state(git_remote_url, coordinator_ref)
    if (
        free_errors
        or not isinstance(free_sha, str)
        or not free_sha
        or not isinstance(free_state, dict)
        or free_state.get("state") != "FREE"
        or free_state.get("released_by_owner") != owner
        or free_state.get("released_transaction_id") != transaction_id
    ):
        print("candidate_executor_remote_recovery_fixture_error mode=resume code=coordinator_release_verify_failed authorized=0 canonical_write=0")
        return 2

    result = {
        "schema_version": SCHEMA_VERSION,
        "recovery_fixture_version": RECOVERY_FIXTURE_VERSION,
        "state": "RECOVERED",
        "fixture_id": fixture_id,
        "transaction_id": transaction_id,
        "owner": owner,
        "coordinator_ref": coordinator_ref,
        "coordinator_restart_read_verified": True,
        "rollback_verified": True,
        "created_target_removed": True,
        "journal_recovered": True,
        "coordinator_released_after_recovery": True,
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }
    if result_path is not None:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_executor_remote_recovery_fixture_resume_ok state=RECOVERED "
        "restart_read=1 rollback_verified=1 created_removed=1 coordinator_released=1 "
        "authorized=0 canonical_write=0"
    )
    return 0


def apply_fixture(remote_root: str, git_remote_url: str, out: Path | None) -> int:
    if not shutil.which("git") or not shutil.which("rclone"):
        print("candidate_executor_remote_recovery_fixture_error code=dependency_missing authorized=0 canonical_write=0")
        return 2

    fixture_id = uuid.uuid4().hex[:24]
    prefix = fixture_prefix(fixture_id)
    ref = executor_fixture_ref(fixture_id)
    transaction_id = sha256((fixture_id + ":recovery-transaction").encode("utf-8"))[:20]
    owner = "executor-recovery-" + fixture_id[:12]

    existing_rel = fixture_rel(fixture_id, EXISTING_NAME)
    created_rel = fixture_rel(fixture_id, CREATE_NAME)
    snapshot_rel = fixture_rel(fixture_id, SNAPSHOT_NAME)
    journal_rel = fixture_rel(fixture_id, JOURNAL_NAME)

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "recovery_fixture_version": RECOVERY_FIXTURE_VERSION,
        "state": "FAILED",
        "fixture_id": fixture_id,
        "fixture_path": prefix,
        "coordinator_ref": ref,
        "transaction_id": transaction_id,
        "owner": owner,
        "coordinator_acquired": False,
        "preconditions_revalidated": False,
        "journal_verified": False,
        "interrupted_state_verified": False,
        "fresh_process_recovery_verified": False,
        "rollback_verified": False,
        "coordinator_released_after_recovery": False,
        "cleanup_verified": False,
        "cleanup_required": True,
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }
    errors: list[str] = []

    try:
        _require(remote_copy_bytes(remote_root, existing_rel, BEFORE_BYTES), "fixture_seed_write_failed")
        _require(remote_cat(remote_root, existing_rel) == BEFORE_BYTES, "fixture_seed_verify_failed")

        base_sha, seed_errors = seed_ref(git_remote_url, ref)
        _require(not seed_errors and isinstance(base_sha, str) and bool(base_sha), "coordinator_seed_failed")
        acquire_work, _, acquire_errors = prepare_owned_acquire(
            git_remote_url,
            ref,
            base_sha,
            transaction_id=transaction_id,
            owner=owner,
        )
        _require(not acquire_errors and acquire_work is not None, "coordinator_acquire_prepare_failed")
        try:
            acquired, _ = push_expected(acquire_work, ref, base_sha)
        finally:
            shutil.rmtree(acquire_work, ignore_errors=True)
        _require(acquired, "coordinator_acquire_cas_failed")
        active_sha, active_state, active_errors = read_ref_state(git_remote_url, ref)
        _require(not active_errors and isinstance(active_sha, str) and bool(active_sha), "coordinator_acquire_readback_failed")
        _require(isinstance(active_state, dict), "coordinator_acquire_state_missing")
        _require(active_state.get("state") == "ACTIVE", "coordinator_not_active")
        _require(active_state.get("transaction_id") == transaction_id, "coordinator_transaction_mismatch")
        _require(active_state.get("owner") == owner, "coordinator_owner_mismatch")
        report["coordinator_acquired"] = True

        _require(remote_cat(remote_root, existing_rel) == BEFORE_BYTES, "live_existing_precondition_changed")
        _require(remote_cat(remote_root, created_rel) is None, "live_create_precondition_changed")
        _require(remote_cat(remote_root, journal_rel) is None, "live_journal_already_exists")
        report["preconditions_revalidated"] = True

        prepared = build_prepared_journal(fixture_id, transaction_id, owner, ref)
        _require(remote_copy_bytes(remote_root, snapshot_rel, BEFORE_BYTES), "snapshot_write_failed")
        _require(remote_copy_json(remote_root, journal_rel, prepared), "journal_write_failed")
        stored = remote_json(remote_root, journal_rel)
        prepared_errors = validate_journal(
            stored,
            fixture_id,
            transaction_id,
            owner,
            ref,
            expected_state="PREPARED",
        )
        _require(not prepared_errors and stored == prepared and remote_cat(remote_root, snapshot_rel) == BEFORE_BYTES, "journal_verify_failed")
        report["journal_verified"] = True

        write_ready = build_write_ready_journal(prepared)
        _require(remote_copy_json(remote_root, journal_rel, write_ready), "write_ready_write_failed")
        stored_ready = remote_json(remote_root, journal_rel)
        ready_errors = validate_journal(
            stored_ready,
            fixture_id,
            transaction_id,
            owner,
            ref,
            expected_state="WRITE_READY_FIXTURE",
        )
        _require(not ready_errors and isinstance(stored_ready, dict) and stored_ready.get("fixture_write_ready") is True, "write_ready_verify_failed")

        _require(remote_copy_bytes(remote_root, existing_rel, AFTER_BYTES), "synthetic_update_write_failed")
        _require(remote_copy_bytes(remote_root, created_rel, CREATED_BYTES), "synthetic_create_write_failed")
        _require(remote_cat(remote_root, existing_rel) == AFTER_BYTES, "synthetic_update_verify_failed")
        _require(remote_cat(remote_root, created_rel) == CREATED_BYTES, "synthetic_create_verify_failed")
        # Important: no receipt, COMMITTED journal or coordinator release is written here.
        report["interrupted_state_verified"] = True

        with tempfile.TemporaryDirectory(prefix="tl-executor-recovery-resume-") as temp:
            result_path = Path(temp) / RESULT_NAME
            child = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--remote",
                    remote_root,
                    "--git-remote-url",
                    git_remote_url,
                    "--resume-recovery",
                    fixture_id,
                    "--transaction-id",
                    transaction_id,
                    "--owner",
                    owner,
                    "--coordinator-ref",
                    ref,
                    "--resume-result",
                    str(result_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=os.environ.copy(),
            )
            _require(child.returncode == 0, "fresh_process_recovery_failed")
            try:
                child_result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                child_result = None
            _require(
                isinstance(child_result, dict)
                and child_result.get("state") == "RECOVERED"
                and child_result.get("coordinator_restart_read_verified") is True
                and child_result.get("rollback_verified") is True
                and child_result.get("created_target_removed") is True
                and child_result.get("journal_recovered") is True
                and child_result.get("coordinator_released_after_recovery") is True
                and child_result.get("production_publish_authorized") is False
                and child_result.get("canonical_write_performed") is False,
                "fresh_process_result_invalid",
            )
        report["fresh_process_recovery_verified"] = True

        terminal = remote_json(remote_root, journal_rel)
        report["rollback_verified"] = (
            remote_cat(remote_root, existing_rel) == BEFORE_BYTES
            and remote_cat(remote_root, created_rel) is None
            and isinstance(terminal, dict)
            and terminal.get("state") == "RECOVERED"
            and terminal.get("rollback_verified") is True
            and terminal.get("restart_recovery_verified") is True
            and terminal.get("removed_created_target_verified") is True
        )
        _require(report["rollback_verified"], "terminal_rollback_verify_failed")

        _, free_state, free_errors = read_ref_state(git_remote_url, ref)
        report["coordinator_released_after_recovery"] = (
            not free_errors
            and isinstance(free_state, dict)
            and free_state.get("state") == "FREE"
            and free_state.get("released_by_owner") == owner
            and free_state.get("released_transaction_id") == transaction_id
        )
        _require(report["coordinator_released_after_recovery"], "terminal_coordinator_release_verify_failed")
        report["state"] = "PASS"

    except RecoveryFixtureFailure as exc:
        errors.append(str(exc))
    finally:
        remote_cleanup = cleanup_remote_prefix(remote_root, prefix)
        current = ls_remote(git_remote_url, ref)
        if current is None:
            ref_cleanup = False
        elif current == "":
            ref_cleanup = True
        else:
            ref_cleanup = not cleanup_ref(git_remote_url, ref, current)
        report["cleanup_verified"] = bool(remote_cleanup and ref_cleanup)
        report["cleanup_required"] = not report["cleanup_verified"]
        if not remote_cleanup:
            errors.append("remote_fixture_cleanup_failed")
        if not ref_cleanup:
            errors.append("coordinator_ref_cleanup_failed")
        if errors:
            report["state"] = "FAILED"

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if errors or report.get("state") != "PASS":
        print(
            "candidate_executor_remote_recovery_fixture_error mode=apply_fixture "
            f"codes={','.join(sorted(set(errors or ['fixture_failed'])))} "
            f"cleanup_required={1 if report.get('cleanup_required') else 0} "
            "authorized=0 canonical_write=0"
        )
        return 2

    print(
        "candidate_executor_remote_recovery_fixture_ok mode=apply_fixture state=PASS "
        "coordinator_acquired=1 preconditions_revalidated=1 journal_verified=1 "
        "interrupted_state=1 fresh_process_recovery=1 rollback_verified=1 "
        "coordinator_released=1 cleanup_verified=1 authorized=0 canonical_write=0"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run executor restart recovery against guarded remote fixtures only.")
    parser.add_argument("--remote", required=True, help="rclone root, e.g. tl:")
    parser.add_argument("--git-remote-url", required=True)
    parser.add_argument("--apply-remote-fixture", action="store_true")
    parser.add_argument("--resume-recovery")
    parser.add_argument("--transaction-id")
    parser.add_argument("--owner")
    parser.add_argument("--coordinator-ref")
    parser.add_argument("--resume-result", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if args.resume_recovery is not None:
        if not all(isinstance(value, str) and value for value in (args.transaction_id, args.owner, args.coordinator_ref)):
            print("candidate_executor_remote_recovery_fixture_error mode=resume code=resume_identity_missing authorized=0 canonical_write=0")
            return 2
        return resume_recovery(
            args.remote,
            args.git_remote_url,
            args.resume_recovery,
            str(args.transaction_id),
            str(args.owner),
            str(args.coordinator_ref),
            args.resume_result,
        )

    if not args.apply_remote_fixture:
        print(
            "candidate_executor_remote_recovery_fixture_ok mode=plan remote_write=0 "
            f"fixture_root={FIXTURE_ROOT} git_ref_prefix=refs/heads/tl-coordination-fixture-executor- "
            "fresh_process_required=1 authorized=0 canonical_write=0"
        )
        return 0

    return apply_fixture(args.remote, args.git_remote_url, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
