#!/usr/bin/env python3
"""Controlled production publication for one exact authorized Technology Library batch.

This is the explicit canonical-write boundary. Apply mode requires:
- the exact Stage 12A preflight artifact;
- the exact readiness + authorization artifacts;
- explicit batch / transaction / confirmation arguments;
- a FREE production coordinator;
- no pending recovery;
- a second live transaction revalidation while ownership is held;
- durable verified snapshots before the first canonical mutation.

The command then performs the exact sealed transaction, rebuilds generated indexes,
verifies canonical bytes, persists a verified receipt + COMMITTED journal, and only
then releases the coordinator.

Any ordinary failure after the write boundary immediately enters deterministic rollback.
A hard process interruption leaves the durable ACTIVE coordinator + recovery journal for
fresh-process recovery, as proven in Stage 11G.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_canonical_index_rebuild import build_index_plan
from candidate_canonical_record_mutation import preflight_record_mutations
from candidate_canonical_transaction_engine import execute_canonical_transaction, post_transaction_record_paths
from candidate_production_coordinator import (
    acquire,
    mark_canonical_write_started,
    read_state as read_coordinator_state,
    release_committed,
    release_prewrite_abort,
    release_recovered,
)
from candidate_production_recovery_engine import execute_recovery
from candidate_publish_authorization import parse_readiness
from candidate_remote_recovery_probe import join_remote, remote_cat, remote_copy_bytes, run_rclone
from candidate_stage_12a_preflight import (
    READY_STATE as STAGE12A_READY_STATE,
    executor_fingerprints,
    transaction_recovery_state,
)
from candidate_transaction_commit_engine import build_committed_journal
from candidate_transaction_prewrite_arm import (
    affected_index_paths,
    build_prepared_journal,
    capture_canonical_state,
    load_live_context,
    mark_aborted_prewrite,
    persist_and_verify_recovery,
    recovery_run_root,
    safe_recovery_path,
)
from candidate_transaction_publish import read_json_file
from candidate_transaction_receipt import build_receipt
from library_index_build import RECORD_NAME_RE

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
PUBLISHER_VERSION = "0.1.0"
CONFIRM_PHRASE = "APPLY_CANONICAL_TRANSACTION"
MASTER = "00_LIBRARY/MASTER_INDEX.md"
MASTER_COUNT_RE = re.compile(rb"(?m)^RECORDS:\s*(\d+)\s*$")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def read_json_bytes(path: Path) -> tuple[dict[str, Any] | None, bytes | None]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, None
    return (value if isinstance(value, dict) else None), raw


def validate_stage12a_preflight(
    preflight: dict[str, Any] | None,
    *,
    preflight_raw: bytes | None,
    readiness_raw: bytes,
    authorization_raw: bytes,
    batch_id: str,
    transaction_id: str,
) -> list[str]:
    if not isinstance(preflight, dict) or preflight_raw is None:
        return ["stage12a_preflight_invalid"]
    errors: list[str] = []
    expected = {
        "schema_version": SCHEMA_VERSION,
        "state": STAGE12A_READY_STATE,
        "batch_id": batch_id,
        "transaction_id": transaction_id,
        "readiness_sha256": sha256(readiness_raw),
        "authorization_sha256": sha256(authorization_raw),
        "coordinator_free": True,
        "pending_recovery": False,
        "authorization_verified": True,
        "live_transaction_revalidated": True,
        "canonical_apply_enabled": False,
        "canonical_write_performed": False,
    }
    for key, value in expected.items():
        if preflight.get(key) != value:
            errors.append(f"stage12a_{key}")

    current_fingerprints, fingerprint_errors = executor_fingerprints()
    errors.extend(fingerprint_errors)
    if preflight.get("executor_source_sha256") != current_fingerprints:
        errors.append("stage12a_executor_source_changed")
    return sorted(set(errors))


def strict_remote_record_paths(remote: str, master_raw: bytes) -> tuple[list[str], list[str]]:
    result = run_rclone(
        [
            "lsjson",
            join_remote(remote, "00_LIBRARY"),
            "--recursive",
            "--files-only",
            "--log-level",
            "ERROR",
        ]
    )
    if result.returncode != 0:
        return [], ["canonical_record_listing_failed"]
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return [], ["canonical_record_listing_invalid_json"]
    if not isinstance(rows, list):
        return [], ["canonical_record_listing_invalid"]

    paths: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        rel = row.get("Path")
        if not isinstance(rel, str):
            continue
        rel = rel.replace("\\", "/").strip("/")
        if RECORD_NAME_RE.fullmatch(Path(rel).name):
            paths.add("00_LIBRARY/" + rel)

    match = MASTER_COUNT_RE.search(master_raw)
    if match is None:
        return [], ["master_record_count_missing"]
    expected_count = int(match.group(1))
    if expected_count <= 0:
        return [], ["master_record_count_invalid"]
    if len(paths) != expected_count:
        return [], [f"canonical_record_count_mismatch:{len(paths)}:{expected_count}"]
    return sorted(paths), []


def planned_index_changes(
    remote: str,
    plan: dict[str, Any],
    canonical_record_paths_before: list[str],
) -> tuple[list[str], list[str]]:
    """Plan post-transaction generated index writes without mutating canonical state."""
    _, expected_targets, preflight_errors = preflight_record_mutations(
        plan,
        lambda path: remote_cat(remote, path),
    )
    if preflight_errors:
        return [], [f"record_preflight:{code}" for code in preflight_errors]

    paths_after = post_transaction_record_paths(plan, canonical_record_paths_before)

    def virtual_reader(path: str) -> bytes | None:
        if path in expected_targets:
            return expected_targets[path]
        return remote_cat(remote, path)

    indexes, _, index_errors = build_index_plan(paths_after, virtual_reader)
    if index_errors or indexes is None:
        return [], index_errors or ["virtual_index_plan_failed"]

    changed = sorted(
        path for path, expected in indexes.items()
        if remote_cat(remote, path) != expected
    )
    allowed = set(affected_index_paths(plan))
    unexpected = sorted(set(changed) - allowed)
    if unexpected:
        return changed, [f"unexpected_index_change_scope:{path}" for path in unexpected]
    return changed, []


def _private_write_exact(remote: str, path: str, raw: bytes) -> bool:
    if not safe_recovery_path(path):
        return False
    if not remote_copy_bytes(remote, path, raw):
        return False
    return remote_cat(remote, path) == raw


def persist_journal(remote: str, journal: dict[str, Any]) -> bool:
    path = journal.get("journal_path")
    if not isinstance(path, str):
        return False
    return _private_write_exact(remote, path, json_bytes(journal))


def canonical_atomic_writer(remote: str, run_root: str):
    counter = {"value": 0}

    def write(path: str, raw: bytes) -> bool:
        if not isinstance(path, str) or not path.startswith("00_LIBRARY/") or ".." in PurePosixPath(path).parts:
            return False
        counter["value"] += 1
        stage = str(
            PurePosixPath(run_root)
            / "stage"
            / f"{counter['value']:04d}-{sha256(path.encode('utf-8'))[:20]}.bin"
        )
        if not _private_write_exact(remote, stage, raw):
            return False
        moved = run_rclone(
            [
                "moveto",
                join_remote(remote, stage),
                join_remote(remote, path),
                "--log-level",
                "ERROR",
                "--stats",
                "0",
            ]
        )
        if moved.returncode != 0:
            return False
        return remote_cat(remote, path) == raw

    return write


def canonical_delete(remote: str, path: str) -> bool:
    if not isinstance(path, str) or not path.startswith("00_LIBRARY/") or ".." in PurePosixPath(path).parts:
        return False
    result = run_rclone(["deletefile", join_remote(remote, path), "--log-level", "ERROR"])
    if result.returncode != 0 and remote_cat(remote, path) is not None:
        return False
    return remote_cat(remote, path) is None


def persist_recovered_and_release(
    *,
    remote: str,
    git_remote_url: str,
    journal: dict[str, Any],
    canonical_writer,
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    journal["state"] = "RECOVERY_REQUIRED"
    journal["canonical_write_performed"] = True
    if not persist_journal(remote, journal):
        return False, ["recovery_required_journal_write_failed"]

    recovered, recovery_errors = execute_recovery(
        journal,
        read_canonical=lambda path: remote_cat(remote, path),
        read_snapshot=lambda path: remote_cat(remote, path),
        write_canonical=canonical_writer,
        delete_canonical=lambda path: canonical_delete(remote, path),
    )
    if recovery_errors or recovered is None:
        return False, recovery_errors or ["recovery_failed"]

    recovered_raw = json_bytes(recovered)
    if not _private_write_exact(remote, str(recovered["journal_path"]), recovered_raw):
        return False, ["recovered_journal_persist_failed"]

    release_errors = release_recovered(
        git_remote_url,
        transaction_id=str(recovered["transaction_id"]),
        batch_id=str(recovered["batch_id"]),
        owner=str(recovered["owner"]),
        journal_path=str(recovered["journal_path"]),
        recovered_journal_sha256=sha256(recovered_raw),
    )
    errors.extend(release_errors)
    return not errors, sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description="Controlled canonical publication for one authorized batch.")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--git-remote-url", required=True)
    parser.add_argument("--readiness", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--stage12a-preflight", type=Path, required=True)
    parser.add_argument("--root", default="99_INBOX/CANDIDATES")
    parser.add_argument("--apply-canonical", action="store_true")
    parser.add_argument("--confirm-batch")
    parser.add_argument("--confirm-transaction")
    parser.add_argument("--confirm-phrase")
    parser.add_argument("--out", type=Path, default=Path("candidate-controlled-publication.json"))
    parser.add_argument("--receipt-out", type=Path, default=Path("candidate-publication-receipt.json"))
    args = parser.parse_args()

    if not shutil.which("rclone") or not shutil.which("git"):
        print("candidate_controlled_publish_error code=dependency_missing canonical_write=0")
        return 2

    try:
        readiness_raw = args.readiness.read_bytes()
        authorization_raw = args.authorization.read_bytes()
        authorization = json.loads(authorization_raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        print("candidate_controlled_publish_error code=local_evidence_read_failed canonical_write=0")
        return 2
    if not isinstance(authorization, dict):
        print("candidate_controlled_publish_error code=authorization_invalid canonical_write=0")
        return 2

    readiness, readiness_errors = parse_readiness(readiness_raw)
    if readiness_errors or readiness is None:
        print(
            "candidate_controlled_publish_error "
            f"codes={','.join(readiness_errors or ['readiness_invalid'])} canonical_write=0"
        )
        return 2

    batch_id = str(readiness["batch_id"])
    transaction_id = str(readiness["transaction_id"])
    preflight, preflight_raw = read_json_bytes(args.stage12a_preflight)
    preflight_errors = validate_stage12a_preflight(
        preflight,
        preflight_raw=preflight_raw,
        readiness_raw=readiness_raw,
        authorization_raw=authorization_raw,
        batch_id=batch_id,
        transaction_id=transaction_id,
    )
    if preflight_errors:
        print(
            "candidate_controlled_publish_error "
            f"codes={','.join(preflight_errors)} canonical_write=0"
        )
        return 3

    context, live_errors = load_live_context(
        remote=str(args.remote),
        root_base=args.root.strip("/"),
        readiness_raw=readiness_raw,
        authorization=authorization,
    )
    if live_errors or context is None:
        print(
            "candidate_controlled_publish_error "
            f"codes={','.join(live_errors or ['live_context_failed'])} canonical_write=0"
        )
        return 3

    plan = context["transaction"]
    record_paths, record_path_errors = strict_remote_record_paths(
        str(args.remote),
        context["master_raw"],
    )
    if record_path_errors:
        print(
            "candidate_controlled_publish_error "
            f"codes={','.join(record_path_errors)} canonical_write=0"
        )
        return 3

    planned_index_writes, index_scope_errors = planned_index_changes(
        str(args.remote),
        plan,
        record_paths,
    )
    if index_scope_errors:
        print(
            "candidate_controlled_publish_error "
            f"codes={','.join(index_scope_errors)} canonical_write=0"
        )
        return 3

    journals, recovery_errors = transaction_recovery_state(str(args.remote), transaction_id)
    if recovery_errors:
        print(
            "candidate_controlled_publish_error "
            f"codes={','.join(recovery_errors)} canonical_write=0"
        )
        return 3

    _, coordinator, coordinator_errors = read_coordinator_state(str(args.git_remote_url))
    if (
        coordinator_errors
        or not isinstance(coordinator, dict)
        or coordinator.get("state") != "FREE"
    ):
        print(
            "candidate_controlled_publish_error "
            f"codes={','.join(coordinator_errors or ['coordinator_not_free'])} canonical_write=0"
        )
        return 3

    counts = plan.get("counts") if isinstance(plan.get("counts"), dict) else {}
    if not args.apply_canonical:
        print(
            "candidate_controlled_publish_ok mode=plan "
            f"batch_id={batch_id} transaction_id={transaction_id} "
            f"records_before={len(record_paths)} writes={counts.get('writes')} "
            f"create={counts.get('create')} update={counts.get('update')} unchanged={counts.get('unchanged')} "
            f"terminal_recovery_journals={len(journals)} index_writes={len(planned_index_writes)} "
            "index_scope_verified=1 apply_canonical=0 canonical_write=0"
        )
        return 0

    confirmation_errors: list[str] = []
    if args.confirm_batch != batch_id:
        confirmation_errors.append("confirm_batch_mismatch")
    if args.confirm_transaction != transaction_id:
        confirmation_errors.append("confirm_transaction_mismatch")
    if args.confirm_phrase != CONFIRM_PHRASE:
        confirmation_errors.append("confirm_phrase_mismatch")
    if confirmation_errors:
        print(
            "candidate_controlled_publish_error "
            f"codes={','.join(confirmation_errors)} canonical_write=0"
        )
        return 3

    owner = "candidate-publisher-" + uuid.uuid4().hex[:12]
    run_root = recovery_run_root(transaction_id, owner)
    journal_path = str(PurePosixPath(run_root) / "journal.json")
    receipt_path = str(PurePosixPath(run_root) / "receipt.json")
    acquired = False
    write_boundary = False
    released = False
    recovery_verified = False
    journal: dict[str, Any] | None = None
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "publisher_version": PUBLISHER_VERSION,
        "state": "FAILED",
        "batch_id": batch_id,
        "transaction_id": transaction_id,
        "owner": owner,
        "run_root": run_root,
        "journal_path": journal_path,
        "receipt_path": receipt_path,
        "publisher_source_sha256": sha256(Path(__file__).read_bytes()),
        "stage12a_preflight_sha256": sha256(preflight_raw or b""),
        "readiness_sha256": sha256(readiness_raw),
        "authorization_sha256": sha256(authorization_raw),
        "transaction_plan_sha256": sha256(context["transaction_raw"]),
        "records_before": len(record_paths),
        "records_after": None,
        "planned_index_writes": planned_index_writes,
        "index_scope_verified": True,
        "coordinator_acquired": False,
        "live_revalidated_under_ownership": False,
        "recovery_material_verified": False,
        "canonical_state_verified": False,
        "receipt_verified": False,
        "journal_committed": False,
        "coordinator_released": False,
        "recovery_verified": False,
        "canonical_write_performed": False,
    }
    errors: list[str] = []
    canonical_writer = canonical_atomic_writer(str(args.remote), run_root)

    try:
        _, active, acquire_errors = acquire(
            str(args.git_remote_url),
            transaction_id=transaction_id,
            batch_id=batch_id,
            owner=owner,
            journal_path=journal_path,
        )
        if acquire_errors or active is None:
            raise RuntimeError(",".join(acquire_errors or ["coordinator_acquire_failed"]))
        acquired = True
        report["coordinator_acquired"] = True

        # Mandatory second live revalidation under exclusive ownership.
        owned_context, owned_errors = load_live_context(
            remote=str(args.remote),
            root_base=args.root.strip("/"),
            readiness_raw=readiness_raw,
            authorization=authorization,
        )
        if owned_errors or owned_context is None:
            raise RuntimeError(",".join(owned_errors or ["owned_revalidation_failed"]))

        owned_paths, owned_path_errors = strict_remote_record_paths(
            str(args.remote),
            owned_context["master_raw"],
        )
        if owned_path_errors:
            raise RuntimeError(",".join(owned_path_errors))
        if owned_paths != record_paths:
            raise RuntimeError("canonical_record_set_changed_under_ownership")

        owned_index_writes, owned_index_scope_errors = planned_index_changes(
            str(args.remote),
            owned_context["transaction"],
            owned_paths,
        )
        if owned_index_scope_errors:
            raise RuntimeError(",".join(owned_index_scope_errors))
        if owned_index_writes != planned_index_writes:
            raise RuntimeError("planned_index_write_set_changed_under_ownership")
        report["planned_index_writes"] = owned_index_writes
        report["index_scope_verified"] = True
        report["live_revalidated_under_ownership"] = True

        before_state = capture_canonical_state(owned_context)
        journal = build_prepared_journal(
            transaction=owned_context["transaction"],
            readiness_raw=readiness_raw,
            authorization_raw=authorization_raw,
            transaction_raw=owned_context["transaction_raw"],
            owner=owner,
            run_root=run_root,
            before_state=before_state,
        )
        recovery_material_errors = persist_and_verify_recovery(
            remote=str(args.remote),
            journal=journal,
            before_state=before_state,
        )
        if recovery_material_errors:
            raise RuntimeError(",".join(recovery_material_errors))
        report["recovery_material_verified"] = True

        # The durable journal crosses the write boundary before any canonical mutation.
        journal["state"] = "WRITING_RECORDS"
        journal["canonical_write_performed"] = True
        if not persist_journal(str(args.remote), journal):
            raise RuntimeError("write_boundary_journal_persist_failed")

        boundary_errors = mark_canonical_write_started(
            str(args.git_remote_url),
            transaction_id=transaction_id,
            batch_id=batch_id,
            owner=owner,
            journal_path=journal_path,
        )
        if boundary_errors:
            raise RuntimeError(",".join(boundary_errors))
        write_boundary = True
        report["canonical_write_performed"] = True

        index_phase_marked = {"value": False}
        def index_writer(path: str, raw: bytes) -> bool:
            if not index_phase_marked["value"]:
                assert journal is not None
                journal["state"] = "REBUILDING_INDEXES"
                if not persist_journal(str(args.remote), journal):
                    return False
                index_phase_marked["value"] = True
            return canonical_writer(path, raw)

        transaction_report, transaction_errors = execute_canonical_transaction(
            owned_context["transaction"],
            canonical_record_paths_before=owned_paths,
            read_bytes=lambda path: remote_cat(str(args.remote), path),
            write_record_bytes=canonical_writer,
            write_index_bytes=index_writer,
        )
        if transaction_errors or transaction_report.get("state") != "CANONICAL_STATE_VERIFIED":
            raise RuntimeError(",".join(transaction_errors or ["canonical_transaction_failed"]))

        journal["state"] = "VERIFYING"
        if not persist_journal(str(args.remote), journal):
            raise RuntimeError("verifying_journal_persist_failed")

        records_after = transaction_report.get("records_after")
        expected_records_after = len(owned_paths) + int(counts.get("create") or 0)
        if records_after != expected_records_after:
            raise RuntimeError("post_transaction_record_count_mismatch")
        report["records_after"] = records_after
        report["canonical_state_verified"] = True

        master_after = remote_cat(str(args.remote), MASTER)
        if master_after is None:
            raise RuntimeError("master_after_missing")

        receipt, receipt_errors = build_receipt(
            owned_context["transaction"],
            owned_context["master_raw"],
            master_after,
            lambda path: remote_cat(str(args.remote), path),
        )
        if receipt_errors or receipt is None:
            raise RuntimeError(",".join(receipt_errors or ["receipt_build_failed"]))
        receipt_raw = json_bytes(receipt)
        if not _private_write_exact(str(args.remote), receipt_path, receipt_raw):
            raise RuntimeError("receipt_persist_failed")
        report["receipt_verified"] = True

        journal["state"] = "COMMITTED_PENDING_RECEIPT"
        if not persist_journal(str(args.remote), journal):
            raise RuntimeError("pending_receipt_journal_persist_failed")

        committed, commit_errors = build_committed_journal(journal, receipt, receipt_raw)
        if commit_errors or committed is None:
            raise RuntimeError(",".join(commit_errors or ["commit_journal_build_failed"]))
        committed_raw = json_bytes(committed)
        if not _private_write_exact(str(args.remote), journal_path, committed_raw):
            raise RuntimeError("committed_journal_persist_failed")
        journal = committed
        report["journal_committed"] = True

        release_errors = release_committed(
            str(args.git_remote_url),
            transaction_id=transaction_id,
            batch_id=batch_id,
            owner=owner,
            journal_path=journal_path,
            receipt_sha256=sha256(receipt_raw),
        )
        if release_errors:
            raise RuntimeError(",".join(release_errors))
        released = True
        report["coordinator_released"] = True

        _, free, free_errors = read_coordinator_state(str(args.git_remote_url))
        if (
            free_errors
            or not isinstance(free, dict)
            or free.get("state") != "FREE"
            or free.get("release_reason") != "VERIFIED_COMMITTED"
        ):
            raise RuntimeError("coordinator_release_readback_failed")

        args.receipt_out.parent.mkdir(parents=True, exist_ok=True)
        args.receipt_out.write_bytes(receipt_raw)
        report["state"] = "COMMITTED"
        report["receipt_sha256"] = sha256(receipt_raw)
        report["master_index_before_sha256"] = sha256(owned_context["master_raw"])
        report["master_index_after_sha256"] = sha256(master_after)

    except RuntimeError as exc:
        errors.extend(code for code in str(exc).split(",") if code)

        if acquired and write_boundary and journal is not None and not released:
            recovered_ok, recovered_errors = persist_recovered_and_release(
                remote=str(args.remote),
                git_remote_url=str(args.git_remote_url),
                journal=journal,
                canonical_writer=canonical_writer,
            )
            recovery_verified = recovered_ok
            report["recovery_verified"] = recovered_ok
            if recovered_errors:
                errors.extend(recovered_errors)
            if recovered_ok:
                released = True
                report["coordinator_released"] = True
                report["state"] = "FAILED_RECOVERED"
        elif acquired and not write_boundary and not released:
            safe_abort_errors: list[str] = []
            if journal is not None:
                safe_abort_errors.extend(mark_aborted_prewrite(str(args.remote), journal))
            if not safe_abort_errors:
                safe_abort_errors.extend(
                    release_prewrite_abort(
                        str(args.git_remote_url),
                        transaction_id=transaction_id,
                        batch_id=batch_id,
                        owner=owner,
                        journal_path=journal_path,
                    )
                )
            errors.extend(safe_abort_errors)
            if not safe_abort_errors:
                released = True
                report["coordinator_released"] = True

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if report.get("state") != "COMMITTED":
        print(
            "candidate_controlled_publish_error "
            f"codes={','.join(sorted(set(errors or ['publication_failed'])))} "
            f"state={report.get('state')} recovery_verified={1 if recovery_verified else 0} "
            f"coordinator_released={1 if released else 0} "
            f"canonical_write={1 if write_boundary else 0}"
        )
        return 5 if recovery_verified else 6

    print(
        "candidate_controlled_publish_ok mode=apply_canonical state=COMMITTED "
        f"batch_id={batch_id} transaction_id={transaction_id} "
        f"records_before={report['records_before']} records_after={report['records_after']} "
        f"index_writes={len(report.get('planned_index_writes') or [])} index_scope_verified=1 "
        f"receipt_verified=1 journal_committed=1 coordinator_released=1 "
        "canonical_write=1"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
