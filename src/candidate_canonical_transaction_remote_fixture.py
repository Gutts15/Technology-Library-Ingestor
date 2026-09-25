#!/usr/bin/env python3
"""Exercise the composed canonical transaction engine over real rclone transport.

This is still a private production-transport fixture, not a publisher. Logical paths
retain their real canonical shapes, but every physical remote read/write is remapped
below:

  99_INBOX/CANDIDATES/CANONICAL_TRANSACTION_REMOTE_FIXTURE/<fixture_id>/

The fixture proves the integrated path:
- record UPDATE / CREATE / UNCHANGED
- deterministic MASTER_INDEX/domain INDEX rebuild
- byte verification of records and indexes
- index-stage failure after record mutation => RECOVERY_REQUIRED
- exact snapshot restoration of records and indexes
- cleanup of all private fixture state

It never writes the real 00_LIBRARY tree.
"""

from __future__ import annotations

import argparse
import json
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_canonical_index_rebuild import build_index_plan
from candidate_canonical_transaction_engine import execute_canonical_transaction
from candidate_remote_recovery_probe import join_remote, remote_cat, remote_copy_bytes, run_rclone

SCHEMA_VERSION = 1
FIXTURE_VERSION = "0.1.0"
FIXTURE_ROOT = "99_INBOX/CANDIDATES/CANONICAL_TRANSACTION_REMOTE_FIXTURE"

EXISTING_TARGET = "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-existing.md"
UNCHANGED_TARGET = "00_LIBRARY/04_AI_AGENTS/TOOLS/technology-unchanged.md"
CREATE_TARGET = "00_LIBRARY/07_DESIGN_CREATIVE/TOOLS/technology-created.md"

UPDATE_CONTENT = "99_INBOX/CANDIDATES/UPDATE_READY/records/update.md"
CREATE_CONTENT = "99_INBOX/CANDIDATES/PUBLISH_READY/records/create.md"
UNCHANGED_CONTENT = "99_INBOX/CANDIDATES/PUBLISH_READY/records/unchanged.md"

MASTER = "00_LIBRARY/MASTER_INDEX.md"
AI_INDEX = "00_LIBRARY/04_AI_AGENTS/INDEX.md"
DESIGN_INDEX = "00_LIBRARY/07_DESIGN_CREATIVE/INDEX.md"


def record(
    rid: str,
    *,
    domain: str,
    category: str,
    title: str,
    slug: str,
    note: str,
) -> tuple[str, bytes]:
    path = f"00_LIBRARY/{domain}/{category}/technology-{slug}.md"
    raw = (
        f"RECORD_ID: {rid}\n"
        "TYPE: TECHNOLOGY\n"
        "STATUS: REFERENCE\n"
        f"DOMAIN: {domain}\n"
        f"CATEGORY: {category}\n"
        f"TITLE: {title}\n"
        f"\n# SUMMARY\n{note}\n"
    ).encode("utf-8")
    return path, raw


_, EXISTING_BEFORE = record(
    "a" * 20,
    domain="04_AI_AGENTS",
    category="TOOLS",
    title="Existing",
    slug="existing",
    note="Before.",
)
_, EXISTING_AFTER = record(
    "a" * 20,
    domain="04_AI_AGENTS",
    category="TOOLS",
    title="Existing",
    slug="existing",
    note="After.",
)
_, UNCHANGED_BYTES = record(
    "b" * 20,
    domain="04_AI_AGENTS",
    category="TOOLS",
    title="Unchanged",
    slug="unchanged",
    note="Stable.",
)
_, CREATE_BYTES = record(
    "c" * 20,
    domain="07_DESIGN_CREATIVE",
    category="TOOLS",
    title="Created",
    slug="created",
    note="Created.",
)


def sha256(raw: bytes) -> str:
    import hashlib
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
        rows = json.loads(check.stdout or "[]")
    except json.JSONDecodeError:
        return False
    return rows == []


def build_plan() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "transaction_plan_version": "0.2.0",
        "transaction_id": sha256(b"canonical-transaction-remote-fixture")[:20],
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
                "candidate_id": "1" * 20,
                "record_id": "a" * 20,
                "target_path": EXISTING_TARGET,
                "content_path": UPDATE_CONTENT,
                "content_sha256": sha256(EXISTING_AFTER),
                "base_sha256": sha256(EXISTING_BEFORE),
                "precondition": "EXACT_BASE_SHA",
            },
            {
                "lane": "NEW",
                "action": "CREATE",
                "package_id": "2" * 20,
                "revision_key": "3" * 20,
                "record_id": "c" * 20,
                "target_path": CREATE_TARGET,
                "content_path": CREATE_CONTENT,
                "content_sha256": sha256(CREATE_BYTES),
                "precondition": "TARGET_ABSENT",
            },
            {
                "lane": "NEW",
                "action": "UNCHANGED",
                "package_id": "4" * 20,
                "revision_key": "5" * 20,
                "record_id": "b" * 20,
                "target_path": UNCHANGED_TARGET,
                "content_path": UNCHANGED_CONTENT,
                "content_sha256": sha256(UNCHANGED_BYTES),
                "precondition": "EXACT_BYTES_PRESENT",
            },
        ],
        "counts": {"create": 1, "update": 1, "unchanged": 1, "writes": 2},
    }


def initial_indexes() -> dict[str, bytes]:
    store = {
        EXISTING_TARGET: EXISTING_BEFORE,
        UNCHANGED_TARGET: UNCHANGED_BYTES,
    }
    indexes, records, errors = build_index_plan(
        [EXISTING_TARGET, UNCHANGED_TARGET],
        lambda path: store.get(path),
    )
    if errors or indexes is None or len(records) != 2:
        raise RuntimeError("initial_index_plan_failed")
    return indexes


def seed(remote: str, fixture_id: str) -> list[str]:
    indexes = initial_indexes()
    seeds = {
        EXISTING_TARGET: EXISTING_BEFORE,
        UNCHANGED_TARGET: UNCHANGED_BYTES,
        UPDATE_CONTENT: EXISTING_AFTER,
        CREATE_CONTENT: CREATE_BYTES,
        UNCHANGED_CONTENT: UNCHANGED_BYTES,
        **indexes,
    }
    errors: list[str] = []
    for logical, raw in seeds.items():
        rel = physical_rel(fixture_id, logical)
        if not remote_copy_bytes(remote, rel, raw):
            errors.append(f"seed_write_failed:{logical}")
        elif remote_cat(remote, rel) != raw:
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


def snapshot(read_bytes) -> dict[str, bytes | None]:
    paths = [
        EXISTING_TARGET,
        UNCHANGED_TARGET,
        CREATE_TARGET,
        MASTER,
        AI_INDEX,
        DESIGN_INDEX,
    ]
    return {path: read_bytes(path) for path in paths}


def restore_snapshot(
    remote: str,
    fixture_id: str,
    before: dict[str, bytes | None],
) -> list[str]:
    errors: list[str] = []
    for logical, raw in before.items():
        rel = physical_rel(fixture_id, logical)
        if raw is None:
            deletion = run_rclone(["deletefile", join_remote(remote, rel), "--log-level", "ERROR"])
            if deletion.returncode != 0 and remote_cat(remote, rel) is not None:
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
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "fixture_version": FIXTURE_VERSION,
        "state": "FAILED",
        "fixture_id": fixture_id,
        "fixture_path": fixture_prefix(fixture_id),
        "success_path_verified": False,
        "indexes_verified": False,
        "index_failure_requires_recovery": False,
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
        plan = build_plan()

        success, success_errors = execute_canonical_transaction(
            plan,
            canonical_record_paths_before=[EXISTING_TARGET, UNCHANGED_TARGET],
            read_bytes=read,
            write_record_bytes=logical_writer(remote, fixture_id),
            write_index_bytes=logical_writer(remote, fixture_id),
        )
        if success_errors or success.get("state") != "CANONICAL_STATE_VERIFIED":
            errors.extend(success_errors or ["integrated_success_path_failed"])
        else:
            report["success_path_verified"] = (
                success.get("records_after") == 3
                and read(EXISTING_TARGET) == EXISTING_AFTER
                and read(CREATE_TARGET) == CREATE_BYTES
                and read(UNCHANGED_TARGET) == UNCHANGED_BYTES
            )
            report["indexes_verified"] = (
                isinstance(read(MASTER), bytes)
                and b"RECORDS: 3" in read(MASTER)
                and read(AI_INDEX) is not None
                and read(DESIGN_INDEX) is not None
            )
            if not report["success_path_verified"]:
                errors.append("integrated_record_verify_failed")
            if not report["indexes_verified"]:
                errors.append("integrated_index_verify_failed")

        if errors:
            return report, sorted(set(errors))

        # Return to exact pre-transaction canonical state for failure exercise.
        initial = initial_indexes()
        reset = {
            EXISTING_TARGET: EXISTING_BEFORE,
            UNCHANGED_TARGET: UNCHANGED_BYTES,
            CREATE_TARGET: None,
            MASTER: initial[MASTER],
            AI_INDEX: initial[AI_INDEX],
            DESIGN_INDEX: None,
        }
        errors.extend(restore_snapshot(remote, fixture_id, reset))
        if errors:
            return report, sorted(set(errors))

        before_failure = snapshot(read)

        failure, failure_errors = execute_canonical_transaction(
            plan,
            canonical_record_paths_before=[EXISTING_TARGET, UNCHANGED_TARGET],
            read_bytes=read,
            write_record_bytes=logical_writer(remote, fixture_id),
            # Let one index write succeed, then fail the next one.
            write_index_bytes=logical_writer(remote, fixture_id, fail_on_call=2),
        )
        report["index_failure_requires_recovery"] = (
            bool(failure_errors)
            and failure.get("state") == "RECOVERY_REQUIRED"
            and failure.get("record_report", {}).get("state") == "RECORDS_VERIFIED"
        )
        if not report["index_failure_requires_recovery"]:
            errors.append("index_failure_contract_failed")

        if not errors:
            errors.extend(restore_snapshot(remote, fixture_id, before_failure))
            report["rollback_verified"] = (
                not errors
                and snapshot(read) == before_failure
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
        description="Run the integrated canonical transaction engine only in private rclone fixture scope."
    )
    parser.add_argument("--remote", required=True)
    parser.add_argument("--apply-remote-fixture", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if not args.apply_remote_fixture:
        print(
            "candidate_canonical_transaction_remote_fixture_ok mode=plan "
            f"fixture_root={FIXTURE_ROOT} remote_write=0 canonical_write=0"
        )
        return 0

    report, errors = run_fixture(str(args.remote))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if errors or report.get("state") != "PASS":
        print(
            "candidate_canonical_transaction_remote_fixture_error "
            f"codes={','.join(errors or ['fixture_failed'])} "
            f"cleanup_verified={1 if report.get('cleanup_verified') else 0} canonical_write=0"
        )
        return 2

    print(
        "candidate_canonical_transaction_remote_fixture_ok mode=apply_fixture state=PASS "
        "records=1 indexes=1 index_failure_requires_recovery=1 rollback_verified=1 "
        "cleanup_verified=1 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
