#!/usr/bin/env python3
"""Exercise the future candidate executor wiring against disposable remote fixtures.

This is NOT a production publisher. It combines the already-proven Git exact-ref
CAS coordinator mechanics with rclone-backed durable recovery material, but writes
only synthetic bytes below ``99_INBOX/CANDIDATES/EXECUTOR_REMOTE_FIXTURE`` and a
random ``tl-coordination-fixture-executor-*`` Git branch.

Success path exercised here:

  seed disposable target -> acquire Git CAS owner -> live revalidate fixture
  preconditions -> persist + verify recovery snapshot/journal -> persist an
  explicit WRITE_READY_FIXTURE state -> mutate only synthetic fixture targets ->
  verify exact bytes -> persist + verify receipt -> mark journal COMMITTED ->
  release owner -> delete disposable Git ref and remote fixture tree.

The hard-interruption/restart path remains a separate follow-up gate. Passing this
fixture proves only success-path wiring. It never reads or writes ``00_LIBRARY`` and
never authorizes production publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_git_ref_remote_probe import (
    REF_PREFIX,
    _prepare_state_commit,
    cleanup_ref,
    ls_remote,
    push_expected,
    read_ref_state,
    seed_ref,
)
from candidate_remote_recovery_probe import (
    join_remote,
    remote_cat,
    remote_copy_bytes,
    remote_copy_json,
    remote_json,
    run_rclone,
)

SCHEMA_VERSION = 1
FIXTURE_VERSION = "0.1.0"
FIXTURE_ROOT = "99_INBOX/CANDIDATES/EXECUTOR_REMOTE_FIXTURE"

EXISTING_NAME = "synthetic-existing.bin"
CREATE_NAME = "synthetic-created.bin"
SNAPSHOT_NAME = "snapshot-existing-before.bin"
JOURNAL_NAME = "journal.json"
RECEIPT_NAME = "receipt.json"

BEFORE_BYTES = b"technology-library-executor-fixture-before\n"
AFTER_BYTES = b"technology-library-executor-fixture-after\n"
CREATED_BYTES = b"technology-library-executor-fixture-created\n"


class FixtureFailure(RuntimeError):
    pass


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def safe_fixture_id(value: str) -> bool:
    return len(value) == 24 and all(ch in "0123456789abcdef" for ch in value)


def fixture_prefix(fixture_id: str) -> str:
    if not safe_fixture_id(fixture_id):
        raise ValueError("unsafe_fixture_id")
    return str(PurePosixPath(FIXTURE_ROOT) / fixture_id)


def fixture_rel(fixture_id: str, name: str) -> str:
    return str(PurePosixPath(fixture_prefix(fixture_id)) / name)


def executor_fixture_ref(fixture_id: str) -> str:
    if not safe_fixture_id(fixture_id):
        raise ValueError("unsafe_fixture_id")
    return REF_PREFIX + "executor-" + fixture_id[:12]


def build_prepared_journal(
    fixture_id: str,
    transaction_id: str,
    owner: str,
    coordinator_ref: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "fixture_version": FIXTURE_VERSION,
        "fixture_only": True,
        "fixture_id": fixture_id,
        "transaction_id": transaction_id,
        "owner": owner,
        "coordinator_ref": coordinator_ref,
        "state": "PREPARED",
        "live_preconditions_verified": True,
        "fixture_write_ready": False,
        "existing_target_path": fixture_rel(fixture_id, EXISTING_NAME),
        "existing_target_before_sha256": sha256(BEFORE_BYTES),
        "existing_target_snapshot_path": fixture_rel(fixture_id, SNAPSHOT_NAME),
        "create_target_path": fixture_rel(fixture_id, CREATE_NAME),
        "create_target_existed_before": False,
        "receipt_path": fixture_rel(fixture_id, RECEIPT_NAME),
        "receipt_verified": False,
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }


def validate_journal(
    payload: dict[str, Any] | None,
    fixture_id: str,
    transaction_id: str,
    owner: str,
    coordinator_ref: str,
    *,
    expected_state: str,
) -> list[str]:
    if not isinstance(payload, dict):
        return ["journal_missing_or_invalid"]
    errors: list[str] = []
    expected_prefix = fixture_prefix(fixture_id) + "/"
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("journal_schema")
    if payload.get("fixture_version") != FIXTURE_VERSION:
        errors.append("journal_version")
    if payload.get("fixture_only") is not True:
        errors.append("journal_fixture_only")
    if payload.get("fixture_id") != fixture_id:
        errors.append("journal_fixture_id")
    if payload.get("transaction_id") != transaction_id:
        errors.append("journal_transaction_id")
    if payload.get("owner") != owner:
        errors.append("journal_owner")
    if payload.get("coordinator_ref") != coordinator_ref:
        errors.append("journal_coordinator_ref")
    if payload.get("state") != expected_state:
        errors.append("journal_state")
    if payload.get("live_preconditions_verified") is not True:
        errors.append("journal_preconditions")
    if payload.get("existing_target_before_sha256") != sha256(BEFORE_BYTES):
        errors.append("journal_before_sha")
    if payload.get("create_target_existed_before") is not False:
        errors.append("journal_create_existed")
    for key in (
        "existing_target_path",
        "existing_target_snapshot_path",
        "create_target_path",
        "receipt_path",
    ):
        value = payload.get(key)
        if not isinstance(value, str) or not value.startswith(expected_prefix):
            errors.append(f"journal_scope:{key}")
    if payload.get("production_publish_authorized") is not False:
        errors.append("journal_authorization_flag")
    if payload.get("canonical_write_performed") is not False:
        errors.append("journal_canonical_write_flag")
    return sorted(set(errors))


def build_receipt(fixture_id: str, transaction_id: str, owner: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "fixture_version": FIXTURE_VERSION,
        "fixture_only": True,
        "fixture_id": fixture_id,
        "transaction_id": transaction_id,
        "owner": owner,
        "state": "VERIFIED_COMMITTED_FIXTURE",
        "targets": [
            {
                "path": fixture_rel(fixture_id, EXISTING_NAME),
                "sha256": sha256(AFTER_BYTES),
            },
            {
                "path": fixture_rel(fixture_id, CREATE_NAME),
                "sha256": sha256(CREATED_BYTES),
            },
        ],
        "recovery_snapshot_sha256": sha256(BEFORE_BYTES),
        "candidate_settlement_eligible": False,
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }


def validate_receipt(
    payload: dict[str, Any] | None,
    fixture_id: str,
    transaction_id: str,
    owner: str,
) -> list[str]:
    if not isinstance(payload, dict):
        return ["receipt_missing_or_invalid"]
    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("receipt_schema")
    if payload.get("fixture_version") != FIXTURE_VERSION:
        errors.append("receipt_version")
    if payload.get("fixture_only") is not True:
        errors.append("receipt_fixture_only")
    if payload.get("fixture_id") != fixture_id:
        errors.append("receipt_fixture_id")
    if payload.get("transaction_id") != transaction_id:
        errors.append("receipt_transaction_id")
    if payload.get("owner") != owner:
        errors.append("receipt_owner")
    if payload.get("state") != "VERIFIED_COMMITTED_FIXTURE":
        errors.append("receipt_state")
    if payload.get("candidate_settlement_eligible") is not False:
        errors.append("receipt_settlement_flag")
    if payload.get("recovery_snapshot_sha256") != sha256(BEFORE_BYTES):
        errors.append("receipt_snapshot_sha")
    if payload.get("production_publish_authorized") is not False:
        errors.append("receipt_authorization_flag")
    if payload.get("canonical_write_performed") is not False:
        errors.append("receipt_canonical_write_flag")

    expected = {
        fixture_rel(fixture_id, EXISTING_NAME): sha256(AFTER_BYTES),
        fixture_rel(fixture_id, CREATE_NAME): sha256(CREATED_BYTES),
    }
    targets = payload.get("targets")
    observed: dict[str, str] = {}
    if not isinstance(targets, list):
        errors.append("receipt_targets")
    else:
        for item in targets:
            if not isinstance(item, dict):
                errors.append("receipt_targets")
                continue
            path = item.get("path")
            digest = item.get("sha256")
            if isinstance(path, str) and isinstance(digest, str):
                observed[path] = digest
            else:
                errors.append("receipt_targets")
    if observed != expected:
        errors.append("receipt_target_binding")
    return sorted(set(errors))


def prepare_owned_acquire(
    remote_url: str,
    ref: str,
    base_sha: str,
    *,
    transaction_id: str,
    owner: str,
) -> tuple[Path | None, str | None, list[str]]:
    return _prepare_state_commit(
        remote_url,
        ref,
        base_sha,
        {
            "state": "ACTIVE",
            "transaction_id": transaction_id,
            "owner": owner,
            "generation": 1,
            "lease_expired_fixture": False,
            "recovery_verified": False,
            "recovered_from_owner": None,
        },
        message=f"fixture: executor acquire {owner}",
    )


def prepare_owned_release(
    remote_url: str,
    ref: str,
    base_sha: str,
    *,
    transaction_id: str,
    owner: str,
    generation: int,
) -> tuple[Path | None, str | None, list[str]]:
    return _prepare_state_commit(
        remote_url,
        ref,
        base_sha,
        {
            "state": "FREE",
            "transaction_id": None,
            "owner": None,
            "generation": generation,
            "lease_expired_fixture": False,
            "recovery_verified": False,
            "recovered_from_owner": None,
            "released_by_owner": owner,
            "released_transaction_id": transaction_id,
        },
        message=f"fixture: executor release {owner}",
    )


def cleanup_remote_prefix(remote_root: str, prefix: str) -> bool:
    if not prefix.startswith(FIXTURE_ROOT + "/"):
        return False
    purge = run_rclone(["purge", join_remote(remote_root, prefix), "--log-level", "ERROR"])
    if purge.returncode != 0:
        return False
    check = run_rclone(["lsjson", join_remote(remote_root, prefix), "--log-level", "ERROR"])
    if check.returncode != 0:
        return True
    try:
        listed = json.loads(check.stdout or "[]")
    except json.JSONDecodeError:
        return False
    return listed == []


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise FixtureFailure(code)


def run_fixture(remote_root: str, git_remote_url: str) -> tuple[dict[str, Any], list[str]]:
    if not shutil.which("git"):
        return {"state": "FAILED", "canonical_write_performed": False}, ["git_missing"]
    if not shutil.which("rclone"):
        return {"state": "FAILED", "canonical_write_performed": False}, ["rclone_missing"]

    fixture_id = uuid.uuid4().hex[:24]
    prefix = fixture_prefix(fixture_id)
    ref = executor_fixture_ref(fixture_id)
    transaction_id = sha256((fixture_id + ":transaction").encode("utf-8"))[:20]
    owner = "executor-fixture-" + fixture_id[:12]

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "fixture_version": FIXTURE_VERSION,
        "state": "FAILED",
        "fixture_id": fixture_id,
        "fixture_path": prefix,
        "coordinator_ref": ref,
        "transaction_id": transaction_id,
        "owner": owner,
        "fixture_seed_verified": False,
        "coordinator_acquired": False,
        "live_preconditions_revalidated": False,
        "recovery_journal_verified": False,
        "write_ready_verified": False,
        "synthetic_transaction_verified": False,
        "receipt_verified": False,
        "journal_committed": False,
        "coordinator_released": False,
        "cleanup_verified": False,
        "cleanup_required": True,
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }
    errors: list[str] = []

    existing_rel = fixture_rel(fixture_id, EXISTING_NAME)
    created_rel = fixture_rel(fixture_id, CREATE_NAME)
    snapshot_rel = fixture_rel(fixture_id, SNAPSHOT_NAME)
    journal_rel = fixture_rel(fixture_id, JOURNAL_NAME)
    receipt_rel = fixture_rel(fixture_id, RECEIPT_NAME)

    try:
        # Fixture setup is intentionally outside the transaction itself. It creates
        # a known pre-state that the executor must later revalidate under ownership.
        _require(remote_copy_bytes(remote_root, existing_rel, BEFORE_BYTES), "fixture_seed_write_failed")
        _require(remote_cat(remote_root, existing_rel) == BEFORE_BYTES, "fixture_seed_verify_failed")
        report["fixture_seed_verified"] = True

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
        _require(active_state.get("lease_expired_fixture") is False, "coordinator_active_marked_stale")
        report["coordinator_acquired"] = True

        # This is the fixture equivalent of the mandatory live pre-write gate.
        _require(remote_cat(remote_root, existing_rel) == BEFORE_BYTES, "live_existing_precondition_changed")
        _require(remote_cat(remote_root, created_rel) is None, "live_create_precondition_changed")
        _require(remote_cat(remote_root, journal_rel) is None, "live_journal_already_exists")
        _require(remote_cat(remote_root, receipt_rel) is None, "live_receipt_already_exists")
        report["live_preconditions_revalidated"] = True

        journal = build_prepared_journal(fixture_id, transaction_id, owner, ref)
        _require(remote_copy_bytes(remote_root, snapshot_rel, BEFORE_BYTES), "snapshot_write_failed")
        _require(remote_copy_json(remote_root, journal_rel, journal), "journal_write_failed")
        _require(remote_cat(remote_root, snapshot_rel) == BEFORE_BYTES, "snapshot_verify_failed")
        stored_journal = remote_json(remote_root, journal_rel)
        journal_errors = validate_journal(
            stored_journal,
            fixture_id,
            transaction_id,
            owner,
            ref,
            expected_state="PREPARED",
        )
        _require(not journal_errors and stored_journal == journal, "journal_verify_failed")
        report["recovery_journal_verified"] = True

        write_ready = dict(journal)
        write_ready["state"] = "WRITE_READY_FIXTURE"
        write_ready["fixture_write_ready"] = True
        _require(remote_copy_json(remote_root, journal_rel, write_ready), "write_ready_write_failed")
        ready_stored = remote_json(remote_root, journal_rel)
        ready_errors = validate_journal(
            ready_stored,
            fixture_id,
            transaction_id,
            owner,
            ref,
            expected_state="WRITE_READY_FIXTURE",
        )
        _require(not ready_errors and isinstance(ready_stored, dict) and ready_stored.get("fixture_write_ready") is True, "write_ready_verify_failed")
        report["write_ready_verified"] = True

        # Synthetic transaction. These paths are deliberately outside 00_LIBRARY.
        _require(remote_copy_bytes(remote_root, existing_rel, AFTER_BYTES), "synthetic_update_write_failed")
        _require(remote_copy_bytes(remote_root, created_rel, CREATED_BYTES), "synthetic_create_write_failed")
        _require(remote_cat(remote_root, existing_rel) == AFTER_BYTES, "synthetic_update_verify_failed")
        _require(remote_cat(remote_root, created_rel) == CREATED_BYTES, "synthetic_create_verify_failed")
        report["synthetic_transaction_verified"] = True

        receipt = build_receipt(fixture_id, transaction_id, owner)
        receipt_errors = validate_receipt(receipt, fixture_id, transaction_id, owner)
        _require(not receipt_errors, "receipt_contract_failed")
        _require(remote_copy_json(remote_root, receipt_rel, receipt), "receipt_write_failed")
        stored_receipt = remote_json(remote_root, receipt_rel)
        stored_receipt_errors = validate_receipt(stored_receipt, fixture_id, transaction_id, owner)
        _require(not stored_receipt_errors and stored_receipt == receipt, "receipt_verify_failed")
        report["receipt_verified"] = True

        committed = dict(write_ready)
        committed["state"] = "COMMITTED"
        committed["fixture_write_ready"] = False
        committed["receipt_verified"] = True
        committed["fixture_transaction_verified"] = True
        committed["receipt_sha256"] = sha256(json_bytes(receipt))
        _require(remote_copy_json(remote_root, journal_rel, committed), "journal_commit_write_failed")
        committed_stored = remote_json(remote_root, journal_rel)
        committed_errors = validate_journal(
            committed_stored,
            fixture_id,
            transaction_id,
            owner,
            ref,
            expected_state="COMMITTED",
        )
        _require(
            not committed_errors
            and isinstance(committed_stored, dict)
            and committed_stored.get("receipt_verified") is True
            and committed_stored.get("fixture_transaction_verified") is True,
            "journal_commit_verify_failed",
        )
        report["journal_committed"] = True

        release_base_sha, release_state, release_read_errors = read_ref_state(git_remote_url, ref)
        _require(not release_read_errors and isinstance(release_base_sha, str) and bool(release_base_sha), "coordinator_release_read_failed")
        _require(isinstance(release_state, dict), "coordinator_release_state_missing")
        _require(release_state.get("state") == "ACTIVE", "coordinator_release_not_active")
        _require(release_state.get("owner") == owner, "coordinator_release_owner_mismatch")
        _require(release_state.get("transaction_id") == transaction_id, "coordinator_release_transaction_mismatch")

        release_work, _, release_errors = prepare_owned_release(
            git_remote_url,
            ref,
            release_base_sha,
            transaction_id=transaction_id,
            owner=owner,
            generation=int(release_state.get("generation") or 0) + 1,
        )
        _require(not release_errors and release_work is not None, "coordinator_release_prepare_failed")
        try:
            released, _ = push_expected(release_work, ref, release_base_sha)
        finally:
            shutil.rmtree(release_work, ignore_errors=True)
        _require(released, "coordinator_release_cas_failed")

        free_sha, free_state, free_errors = read_ref_state(git_remote_url, ref)
        _require(not free_errors and isinstance(free_sha, str) and bool(free_sha), "coordinator_release_readback_failed")
        _require(isinstance(free_state, dict) and free_state.get("state") == "FREE", "coordinator_release_state_invalid")
        _require(free_state.get("released_by_owner") == owner, "coordinator_release_owner_binding")
        _require(free_state.get("released_transaction_id") == transaction_id, "coordinator_release_transaction_binding")
        report["coordinator_released"] = True
        report["state"] = "PASS"

    except FixtureFailure as exc:
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

    return report, sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the guarded remote executor wiring fixture without canonical writes.")
    parser.add_argument("--remote", required=True, help="rclone root, e.g. tl:")
    parser.add_argument("--git-remote-url", required=True)
    parser.add_argument("--apply-remote-fixture", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if not args.apply_remote_fixture:
        print(
            "candidate_executor_remote_wiring_fixture_ok mode=plan remote_write=0 "
            f"fixture_root={FIXTURE_ROOT} git_ref_prefix={REF_PREFIX}executor- "
            "authorized=0 canonical_write=0"
        )
        return 0

    report, errors = run_fixture(args.remote, args.git_remote_url)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if errors or report.get("state") != "PASS":
        print(
            "candidate_executor_remote_wiring_fixture_error "
            f"codes={','.join(errors or ['fixture_failed'])} "
            f"cleanup_required={1 if report.get('cleanup_required') else 0} "
            "authorized=0 canonical_write=0"
        )
        return 2

    print(
        "candidate_executor_remote_wiring_fixture_ok mode=apply_fixture state=PASS "
        "coordinator_acquired=1 preconditions_revalidated=1 journal_verified=1 "
        "write_ready=1 transaction_verified=1 receipt_verified=1 journal_committed=1 "
        "coordinator_released=1 cleanup_verified=1 authorized=0 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
