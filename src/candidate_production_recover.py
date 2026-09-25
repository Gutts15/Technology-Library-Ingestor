#!/usr/bin/env python3
"""Fresh-process production recovery CLI for interrupted canonical publication.

The command reads the live production coordinator, binds it to the exact durable
recovery journal, and chooses one of three safe restart actions:

- pre-write abort/release when the canonical write boundary was never crossed;
- deterministic byte-for-byte rollback when canonical mutation may exist;
- verified coordinator release when rollback already completed but release did not.

Plan mode is read-only. Apply mode requires the exact transaction id and an explicit
confirmation phrase. The command never resumes a partially applied publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from candidate_controlled_publish import canonical_atomic_writer, canonical_delete
from candidate_production_coordinator import (
    read_state,
    release_prewrite_abort,
    release_recovered,
)
from candidate_production_recovery_engine import execute_recovery
from candidate_remote_recovery_probe import remote_cat, remote_copy_bytes
from candidate_transaction_prewrite_arm import mark_aborted_prewrite

RECOVERY_CLI_VERSION = "0.1.0"
CONFIRM_PHRASE = "RECOVER_CANONICAL_TRANSACTION"

ReadBytes = Callable[[str], bytes | None]


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def load_journal(remote: str, path: str) -> tuple[dict[str, Any] | None, list[str]]:
    raw = remote_cat(remote, path)
    if raw is None:
        return None, ["journal_missing"]
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, ["journal_invalid_json"]
    if not isinstance(value, dict):
        return None, ["journal_invalid"]
    return value, []


def validate_identity(
    coordinator: dict[str, Any] | None,
    journal: dict[str, Any] | None,
) -> list[str]:
    if not isinstance(coordinator, dict) or coordinator.get("state") != "ACTIVE":
        return ["coordinator_not_active"]
    if not isinstance(journal, dict):
        return ["journal_invalid"]
    errors: list[str] = []
    for key in ("transaction_id", "batch_id", "owner", "journal_path"):
        if coordinator.get(key) != journal.get(key):
            errors.append(f"identity_mismatch:{key}")
    return sorted(set(errors))


def classify_restart_action(
    coordinator: dict[str, Any] | None,
    journal: dict[str, Any] | None,
) -> tuple[str | None, list[str]]:
    errors = validate_identity(coordinator, journal)
    if errors or not isinstance(coordinator, dict) or not isinstance(journal, dict):
        return None, errors or ["restart_state_invalid"]

    write_started = coordinator.get("canonical_write_performed") is True
    journal_write_started = journal.get("canonical_write_performed") is True
    state = journal.get("state")

    if not write_started:
        if state == "PREPARED" and not journal_write_started:
            return "PREWRITE_ABORT", []
        # The publisher persists WRITING_RECORDS before the coordinator CAS that
        # authorizes canonical mutation. A crash between those two durable steps
        # therefore has journal_write_started=True but coordinator=False. No
        # canonical write is allowed until the coordinator transition succeeds.
        if state == "WRITING_RECORDS" and journal_write_started:
            return "PREWRITE_ABORT", []
        if (
            state == "ABORTED_PREWRITE"
            and journal.get("safe_abort_verified") is True
            and journal.get("coordinator_release_allowed") is True
        ):
            return "RELEASE_PREWRITE", []
        return None, [f"prewrite_journal_state_invalid:{state}"]

    if not journal_write_started:
        return None, ["write_boundary_mismatch"]

    if (
        state == "RECOVERED"
        and journal.get("rollback_verified") is True
        and journal.get("coordinator_release_allowed") is True
    ):
        return "RELEASE_RECOVERED", []

    if state in {
        "WRITING_RECORDS",
        "REBUILDING_INDEXES",
        "VERIFYING",
        "COMMITTED_PENDING_RECEIPT",
        "RECOVERY_REQUIRED",
    }:
        return "RECOVER", []

    if state == "COMMITTED":
        return None, ["committed_journal_must_not_be_recovered"]
    return None, [f"recovery_journal_state_invalid:{state}"]


def verify_recovered_state(journal: dict[str, Any], reader: ReadBytes) -> list[str]:
    errors: list[str] = []
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
            if not isinstance(path, str):
                errors.append(f"{group}_{index}_path")
                continue
            current = reader(path)
            if entry.get("existed_before") is True:
                expected_sha = entry.get("before_sha256")
                if current is None or not isinstance(expected_sha, str) or sha256(current) != expected_sha:
                    errors.append(f"recovered_path_mismatch:{path}")
            elif entry.get("existed_before") is False:
                if current is not None:
                    errors.append(f"recovered_created_path_still_present:{path}")
            else:
                errors.append(f"{group}_{index}_existed_before")

    master = reader("00_LIBRARY/MASTER_INDEX.md")
    expected_master = journal.get("master_index_before_sha256")
    if master is None or not isinstance(expected_master, str) or sha256(master) != expected_master:
        errors.append("master_restore_sha_mismatch")
    return sorted(set(errors))


def persist_exact(remote: str, path: str, raw: bytes) -> bool:
    logical = PurePosixPath(path.replace("\\", "/"))
    if logical.is_absolute() or ".." in logical.parts:
        return False
    normalized = logical.as_posix().strip("/")
    if not normalized.startswith("99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/"):
        return False
    return remote_copy_bytes(remote, normalized, raw) and remote_cat(remote, normalized) == raw


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover one interrupted production candidate publication.")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--git-remote-url", required=True)
    parser.add_argument("--apply-recovery", action="store_true")
    parser.add_argument("--confirm-transaction")
    parser.add_argument("--confirm-phrase")
    parser.add_argument("--out", type=Path, default=Path("candidate-production-recovery.json"))
    args = parser.parse_args()

    if not shutil.which("rclone") or not shutil.which("git"):
        print("candidate_production_recover_error code=dependency_missing canonical_write=0")
        return 2

    _, coordinator, coordinator_errors = read_state(str(args.git_remote_url))
    if coordinator_errors or not isinstance(coordinator, dict):
        print(
            "candidate_production_recover_error "
            f"codes={','.join(coordinator_errors or ['coordinator_read_failed'])} canonical_write=0"
        )
        return 3
    if coordinator.get("state") != "ACTIVE":
        print("candidate_production_recover_ok mode=plan state=NO_RECOVERY_REQUIRED canonical_write=0")
        return 0

    journal_path = coordinator.get("journal_path")
    if not isinstance(journal_path, str):
        print("candidate_production_recover_error code=coordinator_journal_path canonical_write=0")
        return 3
    journal, journal_errors = load_journal(str(args.remote), journal_path)
    if journal_errors or journal is None:
        print(
            "candidate_production_recover_error "
            f"codes={','.join(journal_errors or ['journal_read_failed'])} canonical_write=0"
        )
        return 3

    action, action_errors = classify_restart_action(coordinator, journal)
    if action_errors or action is None:
        print(
            "candidate_production_recover_error "
            f"codes={','.join(action_errors or ['restart_action_failed'])} canonical_write=0"
        )
        return 3

    transaction_id = str(coordinator["transaction_id"])
    if not args.apply_recovery:
        print(
            "candidate_production_recover_ok mode=plan "
            f"transaction_id={transaction_id} action={action} canonical_write=0"
        )
        return 0

    if args.confirm_transaction != transaction_id or args.confirm_phrase != CONFIRM_PHRASE:
        print("candidate_production_recover_error code=confirmation_mismatch canonical_write=0")
        return 3

    errors: list[str] = []
    final_state = ""

    if action in {"PREWRITE_ABORT", "RELEASE_PREWRITE"}:
        if action == "PREWRITE_ABORT":
            errors.extend(
                verify_recovered_state(
                    journal,
                    lambda path: remote_cat(str(args.remote), path),
                )
            )
            if not errors:
                errors.extend(mark_aborted_prewrite(str(args.remote), journal))
        if not errors:
            errors.extend(
                release_prewrite_abort(
                    str(args.git_remote_url),
                    transaction_id=transaction_id,
                    batch_id=str(coordinator["batch_id"]),
                    owner=str(coordinator["owner"]),
                    journal_path=journal_path,
                )
            )
        final_state = "ABORTED_PREWRITE"
    else:
        if action == "RECOVER":
            run_root = str(PurePosixPath(journal_path).parent)
            recovered, recovery_errors = execute_recovery(
                journal,
                read_canonical=lambda path: remote_cat(str(args.remote), path),
                read_snapshot=lambda path: remote_cat(str(args.remote), path),
                write_canonical=canonical_atomic_writer(str(args.remote), run_root),
                delete_canonical=lambda path: canonical_delete(str(args.remote), path),
            )
            errors.extend(recovery_errors)
            if not errors and recovered is not None:
                recovered_raw = json_bytes(recovered)
                if not persist_exact(str(args.remote), journal_path, recovered_raw):
                    errors.append("recovered_journal_persist_failed")
                else:
                    journal = recovered
            elif recovered is None and not errors:
                errors.append("recovery_failed")

        if not errors:
            errors.extend(
                verify_recovered_state(
                    journal,
                    lambda path: remote_cat(str(args.remote), path),
                )
            )
        if not errors:
            recovered_raw = json_bytes(journal)
            errors.extend(
                release_recovered(
                    str(args.git_remote_url),
                    transaction_id=transaction_id,
                    batch_id=str(coordinator["batch_id"]),
                    owner=str(coordinator["owner"]),
                    journal_path=journal_path,
                    recovered_journal_sha256=sha256(recovered_raw),
                )
            )
        final_state = "RECOVERED"

    if errors:
        print(
            "candidate_production_recover_error "
            f"codes={','.join(sorted(set(errors)))} state={final_state or 'FAILED'} canonical_write=1"
        )
        return 4

    _, free, free_errors = read_state(str(args.git_remote_url))
    expected_reason = "PREWRITE_ABORT_VERIFIED" if final_state == "ABORTED_PREWRITE" else "VERIFIED_RECOVERED"
    if (
        free_errors
        or not isinstance(free, dict)
        or free.get("state") != "FREE"
        or free.get("release_reason") != expected_reason
    ):
        print("candidate_production_recover_error code=coordinator_release_verify_failed canonical_write=1")
        return 4

    report = {
        "recovery_cli_version": RECOVERY_CLI_VERSION,
        "state": final_state,
        "transaction_id": transaction_id,
        "batch_id": coordinator.get("batch_id"),
        "action": action,
        "coordinator_released": True,
        "release_reason": expected_reason,
        "canonical_write_performed": final_state == "RECOVERED",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "candidate_production_recover_ok mode=apply "
        f"state={final_state} transaction_id={transaction_id} "
        f"coordinator_released=1 canonical_write={1 if final_state == 'RECOVERED' else 0}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
