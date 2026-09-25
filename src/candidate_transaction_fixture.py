#!/usr/bin/env python3
"""Execute a candidate transaction plan only inside an explicit local test fixture.

This module exists to exercise the future canonical transaction semantics without
creating a production publisher. It requires a dedicated fixture marker, accepts
only a local root, revalidates transaction preconditions, writes fixture records
atomically, rebuilds generated indexes, verifies exact bytes, builds a verified
receipt, and restores the entire affected fixture state on any failure.

It deliberately has no remote/rclone mode. A successful run means the transaction
contract works in a disposable fixture, not that 00_LIBRARY was published.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_transaction_plan import TRANSACTION_PLAN_VERSION, read_json
from candidate_transaction_receipt import build_receipt
from library_index_build import load_records, planned_indexes, write_local_atomic

SCHEMA_VERSION = 1
FIXTURE_VERSION = "0.2.0"
FIXTURE_MARKER = ".technology-library-fixture"
FIXTURE_MARKER_VALUE = "TECHNOLOGY_LIBRARY_TEST_FIXTURE_V1\n"
MAX_ITEMS = 50


class FixtureFailure(RuntimeError):
    pass


def safe_relative(value: str, prefix: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise FixtureFailure("unsafe_relative_path")
    normalized = path.as_posix().strip("/")
    if not normalized.startswith(prefix):
        raise FixtureFailure("path_outside_expected_prefix")
    return normalized


def require_fixture(root: Path) -> None:
    marker = root / FIXTURE_MARKER
    try:
        value = marker.read_text(encoding="utf-8")
    except OSError as exc:
        raise FixtureFailure("fixture_marker_missing") from exc
    if value != FIXTURE_MARKER_VALUE:
        raise FixtureFailure("fixture_marker_invalid")


def read_bytes(root: Path, relative: str) -> bytes | None:
    try:
        return (root / relative).read_bytes()
    except OSError:
        return None


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def index_paths(root: Path) -> set[str]:
    library = root / "00_LIBRARY"
    if not library.exists():
        return set()
    output: set[str] = set()
    master = library / "MASTER_INDEX.md"
    if master.exists():
        output.add(master.relative_to(root).as_posix())
    for path in library.rglob("INDEX.md"):
        if path.is_file():
            output.add(path.relative_to(root).as_posix())
    return output


def snapshot_paths(root: Path, paths: set[str]) -> dict[str, bytes | None]:
    return {path: read_bytes(root, path) for path in sorted(paths)}


def restore_snapshot(
    root: Path,
    record_snapshot: dict[str, bytes | None],
    index_snapshot: dict[str, bytes | None],
) -> list[str]:
    errors: list[str] = []

    old_indexes = set(index_snapshot)
    for path in sorted(index_paths(root) - old_indexes):
        try:
            (root / path).unlink()
        except OSError:
            errors.append(f"rollback_delete_failed:{path}")

    for path, content in {**record_snapshot, **index_snapshot}.items():
        target = root / path
        try:
            if content is None:
                if target.exists():
                    target.unlink()
            else:
                write_local_atomic(target, content)
        except OSError:
            errors.append(f"rollback_restore_failed:{path}")

    for path, expected in {**record_snapshot, **index_snapshot}.items():
        actual = read_bytes(root, path)
        if actual != expected:
            errors.append(f"rollback_verify_failed:{path}")
    return errors


def validate_plan(plan: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(plan, dict):
        return [], ["transaction_plan_invalid"]
    errors: list[str] = []
    if plan.get("schema_version") != SCHEMA_VERSION:
        errors.append("transaction_schema")
    if plan.get("transaction_plan_version") != TRANSACTION_PLAN_VERSION:
        errors.append("transaction_version")
    if plan.get("canonical_write_performed") is not False:
        errors.append("transaction_write_flag")
    if plan.get("requires_live_revalidation") is not True:
        errors.append("live_revalidation_required")
    if plan.get("requires_atomic_record_writes") is not True:
        errors.append("atomic_writes_required")
    if plan.get("requires_rollback_on_partial_failure") not in {True, False}:
        errors.append("rollback_flag_invalid")
    master_sha = plan.get("master_index_sha256")
    if not isinstance(master_sha, str) or len(master_sha) != 64:
        errors.append("master_sha_invalid")
    items = plan.get("items")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        errors.append("transaction_items")
        return [], sorted(set(errors))

    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            errors.append(f"item_{index}_invalid")
            continue
        action = raw.get("action")
        if action not in {"CREATE", "UPDATE", "UNCHANGED"}:
            errors.append(f"item_{index}_action")
            continue
        try:
            target = safe_relative(str(raw.get("target_path") or ""), "00_LIBRARY/")
            content_path = safe_relative(
                str(raw.get("content_path") or ""),
                "99_INBOX/CANDIDATES/",
            )
        except FixtureFailure as exc:
            errors.append(f"item_{index}_{exc}")
            continue
        content_sha = raw.get("content_sha256")
        if not isinstance(content_sha, str) or len(content_sha) != 64:
            errors.append(f"item_{index}_content_sha")
            continue
        base_sha = raw.get("base_sha256")
        if action == "UPDATE" and (not isinstance(base_sha, str) or len(base_sha) != 64):
            errors.append(f"item_{index}_base_sha")
            continue
        normalized.append({**raw, "target_path": target, "content_path": content_path})
    return normalized, sorted(set(errors))


def simulate_transaction(
    root: Path,
    plan: dict[str, Any] | None,
    *,
    fail_after_writes: int | None = None,
    fail_after_index_rebuild: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    root = root.resolve()
    try:
        require_fixture(root)
    except FixtureFailure as exc:
        return None, [str(exc)]

    items, plan_errors = validate_plan(plan)
    if plan_errors or not isinstance(plan, dict):
        return None, plan_errors or ["transaction_plan_invalid"]

    master_rel = "00_LIBRARY/MASTER_INDEX.md"
    master_before = read_bytes(root, master_rel)
    if master_before is None:
        return None, ["master_missing"]
    if sha256(master_before) != plan.get("master_index_sha256"):
        return None, ["master_sha_changed"]

    write_items = [item for item in items if item["action"] in {"CREATE", "UPDATE"}]
    record_paths = {item["target_path"] for item in write_items}
    record_snapshot = snapshot_paths(root, record_paths)
    index_snapshot = snapshot_paths(root, index_paths(root))

    expected_content: dict[str, bytes] = {}
    precondition_errors: list[str] = []
    for index, item in enumerate(items):
        content = read_bytes(root, item["content_path"])
        if content is None:
            precondition_errors.append(f"item_{index}_content_missing")
            continue
        if sha256(content) != item["content_sha256"]:
            precondition_errors.append(f"item_{index}_content_sha_changed")
            continue
        current = read_bytes(root, item["target_path"])
        if item["action"] == "CREATE" and current is not None:
            precondition_errors.append(f"item_{index}_create_target_exists")
        elif item["action"] == "UNCHANGED" and current != content:
            precondition_errors.append(f"item_{index}_unchanged_bytes_changed")
        elif item["action"] == "UPDATE":
            if current is None:
                precondition_errors.append(f"item_{index}_update_target_missing")
            elif sha256(current) != item["base_sha256"]:
                precondition_errors.append(f"item_{index}_update_base_sha_changed")
        expected_content[item["target_path"]] = content
    if precondition_errors:
        return None, sorted(set(precondition_errors))

    writes_done = 0
    indexes_written = 0
    try:
        for item in write_items:
            content = expected_content[item["target_path"]]
            write_local_atomic(root / item["target_path"], content)
            writes_done += 1
            if fail_after_writes is not None and writes_done >= fail_after_writes:
                raise FixtureFailure("injected_failure_after_write")

        records = load_records(root, None)
        indexes = planned_indexes(records)
        for path, content in sorted(indexes.items()):
            if read_bytes(root, path) != content:
                write_local_atomic(root / path, content)
                indexes_written += 1

        if fail_after_index_rebuild:
            raise FixtureFailure("injected_failure_after_index_rebuild")

        for item in items:
            actual = read_bytes(root, item["target_path"])
            if actual != expected_content[item["target_path"]]:
                raise FixtureFailure("target_verify_failed")

        final_records = load_records(root, None)
        expected_indexes = planned_indexes(final_records)
        for path, expected in expected_indexes.items():
            if read_bytes(root, path) != expected:
                raise FixtureFailure("index_verify_failed")

        master_after = read_bytes(root, master_rel)
        if master_after is None:
            raise FixtureFailure("master_after_missing")
        reader = lambda relative: read_bytes(root, relative)
        receipt, receipt_errors = build_receipt(
            plan,
            master_before,
            master_after,
            reader,
        )
        if receipt_errors or receipt is None:
            code = receipt_errors[0] if receipt_errors else "receipt_verify_failed"
            raise FixtureFailure(f"receipt_verify_failed:{code}")

        report = {
            "schema_version": SCHEMA_VERSION,
            "fixture_version": FIXTURE_VERSION,
            "transaction_id": plan.get("transaction_id"),
            "state": "COMMITTED_FIXTURE",
            "writes": writes_done,
            "indexes_written": indexes_written,
            "records_after": len(final_records),
            "receipt_verified": True,
            "receipt": receipt,
            "rollback_performed": False,
            "fixture_write_performed": writes_done > 0 or indexes_written > 0,
            "canonical_write_performed": False,
        }
        return report, []
    except (FixtureFailure, OSError, ValueError) as exc:
        rollback_errors = restore_snapshot(root, record_snapshot, index_snapshot)
        error_code = str(exc) if isinstance(exc, FixtureFailure) else "fixture_execution_failed"
        errors = [error_code, *rollback_errors]
        report = {
            "schema_version": SCHEMA_VERSION,
            "fixture_version": FIXTURE_VERSION,
            "transaction_id": plan.get("transaction_id"),
            "state": "ROLLED_BACK" if not rollback_errors else "ROLLBACK_FAILED",
            "writes_before_failure": writes_done,
            "indexes_written_before_failure": indexes_written,
            "rollback_performed": True,
            "rollback_verified": not rollback_errors,
            "fixture_write_performed": writes_done > 0 or indexes_written > 0,
            "canonical_write_performed": False,
        }
        return report, sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Execute one candidate transaction only in an explicit local test fixture."
    )
    parser.add_argument("--root-dir", type=Path, required=True)
    parser.add_argument("--transaction-plan", type=Path, required=True)
    parser.add_argument("--fail-after-writes", type=int)
    parser.add_argument("--fail-after-index-rebuild", action="store_true")
    parser.add_argument("--report-out", type=Path)
    args = parser.parse_args()

    plan = read_json(args.transaction_plan)
    report, errors = simulate_transaction(
        args.root_dir,
        plan,
        fail_after_writes=args.fail_after_writes,
        fail_after_index_rebuild=args.fail_after_index_rebuild,
    )
    if args.report_out and report is not None:
        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        args.report_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    if errors:
        state = report.get("state") if isinstance(report, dict) else "FAILED"
        print(
            "candidate_transaction_fixture_error "
            f"state={state} codes={','.join(errors)} canonical_write=0"
        )
        return 2
    assert report is not None
    print(
        "candidate_transaction_fixture_ok "
        f"state={report['state']} writes={report['writes']} "
        f"indexes={report['indexes_written']} records={report['records_after']} "
        f"receipt={1 if report.get('receipt_verified') else 0} canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
