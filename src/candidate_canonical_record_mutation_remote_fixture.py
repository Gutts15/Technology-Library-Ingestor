#!/usr/bin/env python3
"""Exercise canonical record mutation over real rclone transport in private fixture scope.

This is a production-transport integration fixture, not a publisher. Logical transaction
paths retain their real canonical shapes (00_LIBRARY/... and 99_INBOX/CANDIDATES/...),
but every physical remote read/write is remapped below:

  99_INBOX/CANDIDATES/CANONICAL_MUTATION_REMOTE_FIXTURE/<fixture_id>/

The fixture proves:
- UPDATE / CREATE / UNCHANGED through rclone callbacks
- exact byte verification after each write
- partial write failure yields RECOVERY_REQUIRED
- durable-style snapshot restoration returns fixture bytes exactly
- cleanup leaves no fixture state behind

It never writes the real 00_LIBRARY tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_canonical_record_mutation import execute_record_mutations
from candidate_remote_recovery_probe import join_remote, remote_cat, remote_copy_bytes, run_rclone

SCHEMA_VERSION = 1
FIXTURE_VERSION = "0.1.0"
FIXTURE_ROOT = "99_INBOX/CANDIDATES/CANONICAL_MUTATION_REMOTE_FIXTURE"

BASE_TARGET = "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-existing.md"
CREATE_TARGET = "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-created.md"
UNCHANGED_TARGET = "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-unchanged.md"
UPDATE_CONTENT = "99_INBOX/CANDIDATES/UPDATE_READY/records/update.md"
CREATE_CONTENT = "99_INBOX/CANDIDATES/PUBLISH_READY/records/create.md"
UNCHANGED_CONTENT = "99_INBOX/CANDIDATES/PUBLISH_READY/records/unchanged.md"

BASE_BYTES = b"TYPE: TECHNOLOGY\nTITLE: Existing Fixture\nVERSION: before\n"
UPDATE_BYTES = b"TYPE: TECHNOLOGY\nTITLE: Existing Fixture\nVERSION: after\n"
CREATE_BYTES = b"TYPE: TECHNOLOGY\nTITLE: Created Fixture\n"
UNCHANGED_BYTES = b"TYPE: TECHNOLOGY\nTITLE: Unchanged Fixture\n"


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
    logical = PurePosixPath(logical_path.replace("\\", "/"))
    if logical.is_absolute() or not logical.parts or ".." in logical.parts:
        raise ValueError("unsafe_logical_path")
    normalized = logical.as_posix().strip("/")
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
        listed = json.loads(check.stdout or "[]")
    except json.JSONDecodeError:
        return False
    return listed == []


def build_plan() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "transaction_plan_version": "0.2.0",
        "transaction_id": sha256(b"candidate-canonical-record-mutation-remote-fixture")[:20],
        "master_index_sha256": "f" * 64,
        "canonical_write_performed": False,
        "requires_live_revalidation": True,
        "requires_atomic_record_writes": True,
        "requires_index_rebuild_after_write": True,
        "requires_byte_verification_after_write": True,
        "requires_rollback_on_partial_failure": True,
        "items": [
            {
                "lane": "UPDATE",
                "action": "UPDATE",
                "candidate_id": "a" * 20,
                "record_id": "b" * 20,
                "target_path": BASE_TARGET,
                "content_path": UPDATE_CONTENT,
                "content_sha256": sha256(UPDATE_BYTES),
                "base_sha256": sha256(BASE_BYTES),
                "precondition": "EXACT_BASE_SHA",
            },
            {
                "lane": "NEW",
                "action": "CREATE",
                "package_id": "c" * 20,
                "revision_key": "d" * 20,
                "record_id": "e" * 20,
                "target_path": CREATE_TARGET,
                "content_path": CREATE_CONTENT,
                "content_sha256": sha256(CREATE_BYTES),
                "precondition": "TARGET_ABSENT",
            },
            {
                "lane": "NEW",
                "action": "UNCHANGED",
                "package_id": "1" * 20,
                "revision_key": "2" * 20,
                "record_id": "3" * 20,
                "target_path": UNCHANGED_TARGET,
                "content_path": UNCHANGED_CONTENT,
                "content_sha256": sha256(UNCHANGED_BYTES),
                "precondition": "EXACT_BYTES_PRESENT",
            },
        ],
        "counts": {"create": 1, "update": 1, "unchanged": 1, "writes": 2},
    }


def seed(remote: str, fixture_id: str) -> list[str]:
    seeds = {
        BASE_TARGET: BASE_BYTES,
        UNCHANGED_TARGET: UNCHANGED_BYTES,
        UPDATE_CONTENT: UPDATE_BYTES,
        CREATE_CONTENT: CREATE_BYTES,
        UNCHANGED_CONTENT: UNCHANGED_BYTES,
    }
    errors: list[str] = []
    for logical, raw in seeds.items():
        rel = physical_rel(fixture_id, logical)
        if not remote_copy_bytes(remote, rel, raw):
            errors.append(f"seed_write_failed:{logical}")
            continue
        if remote_cat(remote, rel) != raw:
            errors.append(f"seed_verify_failed:{logical}")
    return sorted(set(errors))


def logical_reader(remote: str, fixture_id: str):
    def read(path: str) -> bytes | None:
        try:
            rel = physical_rel(fixture_id, path)
        except ValueError:
            return None
        return remote_cat(remote, rel)
    return read


def logical_writer(remote: str, fixture_id: str, *, fail_on_call: int | None = None):
    calls = {"count": 0}

    def write(path: str, raw: bytes) -> bool:
        calls["count"] += 1
        if fail_on_call is not None and calls["count"] == fail_on_call:
            return False
        try:
            rel = physical_rel(fixture_id, path)
        except ValueError:
            return False
        return remote_copy_bytes(remote, rel, raw)

    return write


def restore_snapshot(
    remote: str,
    fixture_id: str,
    snapshot: dict[str, bytes | None],
) -> list[str]:
    errors: list[str] = []
    for logical, raw in snapshot.items():
        rel = physical_rel(fixture_id, logical)
        if raw is None:
            delete = run_rclone(["deletefile", join_remote(remote, rel), "--log-level", "ERROR"])
            if delete.returncode != 0 and remote_cat(remote, rel) is not None:
                errors.append(f"restore_delete_failed:{logical}")
            elif remote_cat(remote, rel) is not None:
                errors.append(f"restore_delete_verify_failed:{logical}")
        else:
            if not remote_copy_bytes(remote, rel, raw):
                errors.append(f"restore_write_failed:{logical}")
            elif remote_cat(remote, rel) != raw:
                errors.append(f"restore_verify_failed:{logical}")
    return sorted(set(errors))


def run_fixture(remote: str) -> tuple[dict[str, Any], list[str]]:
    if not shutil.which("rclone"):
        return {"state": "FAILED", "canonical_write_performed": False}, ["rclone_missing"]

    fixture_id = uuid.uuid4().hex[:24]
    plan = build_plan()
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "fixture_version": FIXTURE_VERSION,
        "state": "FAILED",
        "fixture_id": fixture_id,
        "fixture_path": fixture_prefix(fixture_id),
        "success_path_verified": False,
        "partial_failure_state_verified": False,
        "rollback_verified": False,
        "cleanup_verified": False,
        "canonical_write_performed": False,
    }
    errors: list[str] = []

    try:
        errors.extend(seed(remote, fixture_id))
        if errors:
            return report, sorted(set(errors))

        read = logical_reader(remote, fixture_id)
        write = logical_writer(remote, fixture_id)
        success, success_errors = execute_record_mutations(
            plan,
            read_bytes=read,
            write_bytes=write,
        )
        if success_errors or not isinstance(success, dict) or success.get("state") != "RECORDS_VERIFIED":
            errors.extend(success_errors or ["success_path_failed"])
        else:
            report["success_path_verified"] = (
                success.get("writes_completed") == 2
                and read(BASE_TARGET) == UPDATE_BYTES
                and read(CREATE_TARGET) == CREATE_BYTES
                and read(UNCHANGED_TARGET) == UNCHANGED_BYTES
            )
            if not report["success_path_verified"]:
                errors.append("success_bytes_verify_failed")

        if errors:
            return report, sorted(set(errors))

        # Reset fixture to the exact original state before failure-path exercise.
        reset_snapshot = {
            BASE_TARGET: BASE_BYTES,
            CREATE_TARGET: None,
            UNCHANGED_TARGET: UNCHANGED_BYTES,
        }
        errors.extend(restore_snapshot(remote, fixture_id, reset_snapshot))
        if errors:
            return report, sorted(set(errors))

        before_failure = {
            BASE_TARGET: read(BASE_TARGET),
            CREATE_TARGET: read(CREATE_TARGET),
            UNCHANGED_TARGET: read(UNCHANGED_TARGET),
        }
        failing_write = logical_writer(remote, fixture_id, fail_on_call=2)
        failed, failure_errors = execute_record_mutations(
            plan,
            read_bytes=read,
            write_bytes=failing_write,
        )
        report["partial_failure_state_verified"] = (
            failure_errors == ["item_1_write_failed"]
            and isinstance(failed, dict)
            and failed.get("state") == "RECOVERY_REQUIRED"
            and failed.get("writes_completed") == 1
            and read(BASE_TARGET) == UPDATE_BYTES
            and read(CREATE_TARGET) is None
        )
        if not report["partial_failure_state_verified"]:
            errors.append("partial_failure_contract_failed")

        if not errors:
            errors.extend(restore_snapshot(remote, fixture_id, before_failure))
            report["rollback_verified"] = (
                not errors
                and read(BASE_TARGET) == BASE_BYTES
                and read(CREATE_TARGET) is None
                and read(UNCHANGED_TARGET) == UNCHANGED_BYTES
            )
            if not report["rollback_verified"]:
                errors.append("rollback_verify_failed")

        if not errors:
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
        description="Run rclone-backed canonical mutation semantics only in private fixture scope."
    )
    parser.add_argument("--remote", required=True)
    parser.add_argument("--apply-remote-fixture", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if not args.apply_remote_fixture:
        print(
            "candidate_canonical_record_mutation_remote_fixture_ok mode=plan "
            f"fixture_root={FIXTURE_ROOT} remote_write=0 canonical_write=0"
        )
        return 0

    report, errors = run_fixture(str(args.remote))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if errors or report.get("state") != "PASS":
        print(
            "candidate_canonical_record_mutation_remote_fixture_error "
            f"codes={','.join(errors or ['fixture_failed'])} "
            f"cleanup_verified={1 if report.get('cleanup_verified') else 0} canonical_write=0"
        )
        return 2

    print(
        "candidate_canonical_record_mutation_remote_fixture_ok mode=apply_fixture state=PASS "
        "success_path=1 partial_failure_requires_recovery=1 rollback_verified=1 "
        "cleanup_verified=1 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
