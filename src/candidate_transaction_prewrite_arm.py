#!/usr/bin/env python3
"""Arm the authorized production transaction through the pre-write boundary only.

This stage proves the production coordinator + durable recovery material against the
real authorized transaction while remaining structurally unable to mutate 00_LIBRARY.

Plan mode performs read-only live validation. Apply-prewrite mode acquires the
production Git exact-ref coordinator, revalidates the transaction under ownership,
persists and verifies private recovery snapshots/journal, verifies canonical state is
unchanged, records ABORTED_PREWRITE, and releases the coordinator.

There is deliberately no --apply-canonical option in this version.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_batch_inspect import (
    DEFAULT_BATCH,
    DEFAULT_MASTER,
    DEFAULT_PUBLISH,
    DEFAULT_ROOT,
    DEFAULT_UPDATE,
    DEFAULT_VALIDATION,
    inspect_batch,
    read_json_bytes,
    read_remote,
)
from candidate_production_coordinator import (
    COORDINATOR_REF,
    acquire,
    read_state as read_coordinator_state,
    release_prewrite_abort,
)
from candidate_publish_authorization import parse_readiness
from candidate_remote_recovery_probe import remote_cat, remote_copy_bytes, remote_copy_json, remote_json
from candidate_transaction_prepare import DEFAULT_TRANSACTION_READY, parse_update_manifest, prepare_transaction
from candidate_transaction_publish import evaluate_publish_inputs, read_json_file

SCHEMA_VERSION = 1
PREWRITE_ARM_VERSION = "0.1.0"
RECOVERY_ROOT = "99_INBOX/CANDIDATES/PRODUCTION_RECOVERY"


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def safe_recovery_path(path: str) -> bool:
    prefix = RECOVERY_ROOT + "/"
    return path.startswith(prefix) and ".." not in PurePosixPath(path).parts


def recovery_run_root(transaction_id: str, owner: str) -> str:
    if len(transaction_id) != 20 or any(ch not in "0123456789abcdef" for ch in transaction_id):
        raise ValueError("unsafe_transaction_id")
    if not owner.startswith("candidate-publisher-") or len(owner) != len("candidate-publisher-") + 12:
        raise ValueError("unsafe_owner")
    return str(PurePosixPath(RECOVERY_ROOT) / transaction_id / owner)


def affected_index_paths(transaction: dict[str, Any]) -> list[str]:
    paths = {"00_LIBRARY/MASTER_INDEX.md"}
    for item in transaction.get("items", []):
        if not isinstance(item, dict):
            continue
        target = item.get("target_path")
        if not isinstance(target, str) or not target.startswith("00_LIBRARY/"):
            continue
        parts = PurePosixPath(target).parts
        if len(parts) >= 3 and parts[1] == "SOURCES":
            paths.add("00_LIBRARY/SOURCES/INDEX.md")
        elif len(parts) >= 4:
            paths.add(str(PurePosixPath("00_LIBRARY") / parts[1] / "INDEX.md"))
    return sorted(paths)


def snapshot_name(path: str) -> str:
    return sha256(path.encode("utf-8"))[:20] + ".bin"


def load_live_context(
    *,
    remote: str,
    root_base: str,
    readiness_raw: bytes,
    authorization: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    publish_rel = f"{root_base}/{DEFAULT_PUBLISH}"
    validation_rel = f"{root_base}/{DEFAULT_VALIDATION}"
    update_rel = f"{root_base}/{DEFAULT_UPDATE}"
    batch_rel = f"{root_base}/{DEFAULT_BATCH}"
    transaction_rel = f"{root_base}/{DEFAULT_TRANSACTION_READY}"
    reader = lambda relative: read_remote(remote, relative)

    master_raw = reader(DEFAULT_MASTER)
    publish_raw = reader(publish_rel)
    validation_raw = reader(validation_rel)
    update_raw = reader(update_rel)
    batch_raw = reader(batch_rel)
    transaction_raw = reader(transaction_rel)

    batch_report, batch_errors = inspect_batch(
        master_raw=master_raw,
        publish_raw=publish_raw,
        validation_raw=validation_raw,
        update_raw=update_raw,
        batch_raw=batch_raw,
        read_content=reader,
    )
    if batch_errors or batch_report is None or master_raw is None:
        return None, batch_errors or ["batch_inspection_failed"]

    updates, update_errors = parse_update_manifest(read_json_bytes(update_raw))
    if update_errors:
        return None, update_errors

    recomputed_transaction, transaction_errors = prepare_transaction(
        master_content=master_raw,
        new_manifest=read_json_bytes(publish_raw),
        new_validation=read_json_bytes(validation_raw),
        update_artifacts=updates,
        read_content=reader,
        finalize_batch_id=str(batch_report.get("batch_id")),
    )
    if transaction_errors or recomputed_transaction is None:
        return None, transaction_errors or ["transaction_recompute_failed"]

    stored_transaction = read_json_bytes(transaction_raw)
    input_errors = evaluate_publish_inputs(
        readiness_raw=readiness_raw,
        authorization=authorization,
        batch_report=batch_report,
        stored_transaction=stored_transaction,
        recomputed_transaction=recomputed_transaction,
    )
    if input_errors:
        return None, input_errors
    if not isinstance(stored_transaction, dict) or transaction_raw is None:
        return None, ["transaction_missing"]

    return {
        "batch_report": batch_report,
        "transaction": stored_transaction,
        "transaction_raw": transaction_raw,
        "master_raw": master_raw,
        "reader": reader,
    }, []


def capture_canonical_state(context: dict[str, Any]) -> dict[str, bytes | None]:
    reader = context["reader"]
    transaction = context["transaction"]
    paths = set(affected_index_paths(transaction))
    for item in transaction.get("items", []):
        if isinstance(item, dict) and isinstance(item.get("target_path"), str):
            paths.add(item["target_path"])
    return {path: reader(path) for path in sorted(paths)}


def build_prepared_journal(
    *,
    transaction: dict[str, Any],
    readiness_raw: bytes,
    authorization_raw: bytes,
    transaction_raw: bytes,
    owner: str,
    run_root: str,
    before_state: dict[str, bytes | None],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for item in transaction.get("items", []):
        if not isinstance(item, dict):
            continue
        target = str(item.get("target_path"))
        current = before_state.get(target)
        items.append(
            {
                "action": item.get("action"),
                "record_id": item.get("record_id"),
                "target_path": target,
                "existed_before": current is not None,
                "before_sha256": sha256(current) if current is not None else None,
                "snapshot_path": (
                    str(PurePosixPath(run_root) / "snapshots" / snapshot_name(target))
                    if current is not None else None
                ),
            }
        )

    indexes: list[dict[str, Any]] = []
    for path in affected_index_paths(transaction):
        current = before_state.get(path)
        indexes.append(
            {
                "path": path,
                "existed_before": current is not None,
                "before_sha256": sha256(current) if current is not None else None,
                "snapshot_path": (
                    str(PurePosixPath(run_root) / "snapshots" / snapshot_name(path))
                    if current is not None else None
                ),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "prewrite_arm_version": PREWRITE_ARM_VERSION,
        "state": "PREPARED",
        "transaction_id": transaction.get("transaction_id"),
        "batch_id": transaction.get("finalize_batch_id"),
        "owner": owner,
        "coordinator_ref": COORDINATOR_REF,
        "run_root": run_root,
        "journal_path": str(PurePosixPath(run_root) / "journal.json"),
        "readiness_sha256": sha256(readiness_raw),
        "authorization_sha256": sha256(authorization_raw),
        "transaction_plan_sha256": sha256(transaction_raw),
        "master_index_before_sha256": transaction.get("master_index_sha256"),
        "live_preconditions_verified_under_ownership": True,
        "snapshots_verified": False,
        "safe_abort_verified": False,
        "coordinator_release_allowed": False,
        "canonical_write_performed": False,
        "items": items,
        "indexes": indexes,
    }


def _private_write_bytes(remote: str, relative: str, raw: bytes) -> bool:
    if not safe_recovery_path(relative):
        return False
    return remote_copy_bytes(remote, relative, raw)


def _private_write_json(remote: str, relative: str, payload: dict[str, Any]) -> bool:
    if not safe_recovery_path(relative):
        return False
    return remote_copy_json(remote, relative, payload)


def persist_and_verify_recovery(
    *,
    remote: str,
    journal: dict[str, Any],
    before_state: dict[str, bytes | None],
) -> list[str]:
    errors: list[str] = []
    for group in ("items", "indexes"):
        for entry in journal.get(group, []):
            if not isinstance(entry, dict):
                errors.append("journal_entry_invalid")
                continue
            source_path = entry.get("target_path") if group == "items" else entry.get("path")
            snapshot_path = entry.get("snapshot_path")
            if snapshot_path is None:
                continue
            raw = before_state.get(str(source_path))
            if raw is None:
                errors.append(f"snapshot_source_missing:{source_path}")
                continue
            if not _private_write_bytes(remote, str(snapshot_path), raw):
                errors.append(f"snapshot_write_failed:{source_path}")
                continue
            if remote_cat(remote, str(snapshot_path)) != raw:
                errors.append(f"snapshot_verify_failed:{source_path}")

    if errors:
        return sorted(set(errors))

    prepared = dict(journal)
    prepared["snapshots_verified"] = True
    journal_path = str(prepared["journal_path"])
    if not _private_write_json(remote, journal_path, prepared):
        return ["journal_write_failed"]
    stored = remote_json(remote, journal_path)
    if stored != prepared:
        return ["journal_verify_failed"]
    journal.clear()
    journal.update(prepared)
    return []


def mark_aborted_prewrite(remote: str, journal: dict[str, Any]) -> list[str]:
    terminal = dict(journal)
    terminal["state"] = "ABORTED_PREWRITE"
    terminal["safe_abort_verified"] = True
    terminal["coordinator_release_allowed"] = True
    terminal["canonical_write_performed"] = False
    journal_path = str(terminal.get("journal_path"))
    if not _private_write_json(remote, journal_path, terminal):
        return ["abort_journal_write_failed"]
    stored = remote_json(remote, journal_path)
    if stored != terminal:
        return ["abort_journal_verify_failed"]
    journal.clear()
    journal.update(terminal)
    return []


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prove production coordinator + recovery preparation without canonical writes."
    )
    parser.add_argument("--remote", required=True)
    parser.add_argument("--git-remote-url")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--readiness", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--apply-prewrite-arm", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("candidate_transaction_prewrite_arm_error code=rclone_missing canonical_write=0")
        return 2
    try:
        readiness_raw = args.readiness.read_bytes()
        authorization_raw = args.authorization.read_bytes()
        authorization = json.loads(authorization_raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        print("candidate_transaction_prewrite_arm_error code=local_evidence_read_failed canonical_write=0")
        return 2
    if not isinstance(authorization, dict):
        print("candidate_transaction_prewrite_arm_error code=authorization_invalid canonical_write=0")
        return 2

    context, errors = load_live_context(
        remote=str(args.remote),
        root_base=args.root.strip("/"),
        readiness_raw=readiness_raw,
        authorization=authorization,
    )
    if errors or context is None:
        print(
            "candidate_transaction_prewrite_arm_error "
            f"codes={','.join(errors or ['live_context_failed'])} canonical_write=0"
        )
        return 3

    transaction = context["transaction"]
    counts = transaction.get("counts") if isinstance(transaction.get("counts"), dict) else {}
    if not args.apply_prewrite_arm:
        print(
            "candidate_transaction_prewrite_arm_ok mode=plan "
            f"transaction_id={transaction.get('transaction_id')} "
            f"writes={counts.get('writes')} recovery_indexes={len(affected_index_paths(transaction))} "
            "remote_private_write=0 coordinator_write=0 canonical_write=0"
        )
        return 0

    if not args.git_remote_url:
        print("candidate_transaction_prewrite_arm_error code=git_remote_url_required canonical_write=0")
        return 2
    if not shutil.which("git"):
        print("candidate_transaction_prewrite_arm_error code=git_missing canonical_write=0")
        return 2

    readiness, readiness_errors = parse_readiness(readiness_raw)
    if readiness_errors or readiness is None:
        print("candidate_transaction_prewrite_arm_error code=readiness_invalid canonical_write=0")
        return 2

    owner = "candidate-publisher-" + uuid.uuid4().hex[:12]
    run_root = recovery_run_root(str(readiness["transaction_id"]), owner)
    journal_path = str(PurePosixPath(run_root) / "journal.json")
    acquired = False
    released = False
    journal: dict[str, Any] | None = None
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "prewrite_arm_version": PREWRITE_ARM_VERSION,
        "state": "FAILED",
        "transaction_id": readiness["transaction_id"],
        "batch_id": readiness["batch_id"],
        "owner": owner,
        "run_root": run_root,
        "journal_path": journal_path,
        "coordinator_ref": COORDINATOR_REF,
        "coordinator_acquired": False,
        "live_revalidated_under_ownership": False,
        "recovery_material_verified": False,
        "canonical_state_unchanged": False,
        "safe_abort_verified": False,
        "coordinator_released": False,
        "canonical_write_performed": False,
    }
    run_errors: list[str] = []

    try:
        _, active, acquire_errors = acquire(
            str(args.git_remote_url),
            transaction_id=str(readiness["transaction_id"]),
            batch_id=str(readiness["batch_id"]),
            owner=owner,
            journal_path=journal_path,
        )
        if acquire_errors or active is None:
            raise RuntimeError(",".join(acquire_errors or ["coordinator_acquire_failed"]))
        acquired = True
        report["coordinator_acquired"] = True

        # Mandatory second live read happens after exclusive ownership is held.
        owned_context, owned_errors = load_live_context(
            remote=str(args.remote),
            root_base=args.root.strip("/"),
            readiness_raw=readiness_raw,
            authorization=authorization,
        )
        if owned_errors or owned_context is None:
            raise RuntimeError(",".join(owned_errors or ["owned_revalidation_failed"]))
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
        recovery_errors = persist_and_verify_recovery(
            remote=str(args.remote),
            journal=journal,
            before_state=before_state,
        )
        if recovery_errors:
            raise RuntimeError(",".join(recovery_errors))
        report["recovery_material_verified"] = True

        after_state = capture_canonical_state(owned_context)
        if after_state != before_state:
            raise RuntimeError("canonical_state_changed_during_prewrite")
        report["canonical_state_unchanged"] = True

        abort_errors = mark_aborted_prewrite(str(args.remote), journal)
        if abort_errors:
            raise RuntimeError(",".join(abort_errors))
        report["safe_abort_verified"] = True

        release_errors = release_prewrite_abort(
            str(args.git_remote_url),
            transaction_id=str(readiness["transaction_id"]),
            batch_id=str(readiness["batch_id"]),
            owner=owner,
            journal_path=journal_path,
        )
        if release_errors:
            raise RuntimeError(",".join(release_errors))
        released = True
        report["coordinator_released"] = True

        _, free_state, free_errors = read_coordinator_state(str(args.git_remote_url))
        if free_errors or not isinstance(free_state, dict) or free_state.get("state") != "FREE":
            raise RuntimeError("coordinator_release_readback_failed")

        report["state"] = "PASS"

    except RuntimeError as exc:
        run_errors.extend(code for code in str(exc).split(",") if code)
    finally:
        # This version has no canonical mutation functions. If an ordinary error
        # happens before release, convert any persisted journal to a verified
        # pre-write abort and attempt to release the owner. A hard process kill may
        # still leave ACTIVE state; later restart-recovery work must handle that.
        if acquired and not released:
            safe_to_release = True
            if journal is not None:
                terminal_errors = mark_aborted_prewrite(str(args.remote), journal)
                if terminal_errors:
                    safe_to_release = False
                    run_errors.extend(terminal_errors)
            if safe_to_release:
                release_errors = release_prewrite_abort(
                    str(args.git_remote_url),
                    transaction_id=str(readiness["transaction_id"]),
                    batch_id=str(readiness["batch_id"]),
                    owner=owner,
                    journal_path=journal_path,
                )
                if release_errors:
                    run_errors.extend(release_errors)
                else:
                    released = True
                    report["coordinator_released"] = True

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if run_errors or report.get("state") != "PASS":
        print(
            "candidate_transaction_prewrite_arm_error "
            f"codes={','.join(sorted(set(run_errors or ['prewrite_failed'])))} "
            f"coordinator_released={1 if released else 0} canonical_write=0"
        )
        return 4

    print(
        "candidate_transaction_prewrite_arm_ok mode=apply_prewrite state=PASS "
        f"transaction_id={readiness['transaction_id']} "
        "coordinator_acquired=1 live_revalidated=1 recovery_verified=1 "
        "canonical_unchanged=1 safe_abort=1 coordinator_released=1 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
