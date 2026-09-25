#!/usr/bin/env python3
"""Execute candidate lifecycle settlement only inside an explicit local fixture.

The canonical transaction itself must already be represented by a verified
settlement plan. This module moves only private candidate files from
READY_FOR_CURATION to RESOLVED/PUBLISHED/<transaction_id>, writes one private
settlement state document, and supports idempotent recovery after a partial move.

It has no remote mode and never writes to 00_LIBRARY.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_settlement_plan import SETTLEMENT_PLAN_VERSION
from candidate_transaction_plan import read_json
from candidate_transaction_fixture import FIXTURE_MARKER, FIXTURE_MARKER_VALUE

SCHEMA_VERSION = 1
SETTLEMENT_FIXTURE_VERSION = "0.1.0"
CANDIDATE_ROOT = "99_INBOX/CANDIDATES"
MAX_ITEMS = 50


class SettlementFailure(RuntimeError):
    pass


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def require_fixture(root: Path) -> None:
    marker = root / FIXTURE_MARKER
    try:
        value = marker.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettlementFailure("fixture_marker_missing") from exc
    if value != FIXTURE_MARKER_VALUE:
        raise SettlementFailure("fixture_marker_invalid")


def safe_candidate_relative(value: Any, prefix: str) -> str:
    if not isinstance(value, str):
        raise SettlementFailure("candidate_path_invalid")
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise SettlementFailure("candidate_path_invalid")
    normalized = path.as_posix().strip("/")
    if not normalized.startswith(prefix):
        raise SettlementFailure("candidate_path_outside_bucket")
    return normalized


def validate_plan(plan: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(plan, dict):
        return [], ["settlement_plan_invalid"]
    errors: list[str] = []
    if plan.get("schema_version") != SCHEMA_VERSION:
        errors.append("settlement_schema")
    if plan.get("settlement_plan_version") != SETTLEMENT_PLAN_VERSION:
        errors.append("settlement_version")
    if plan.get("canonical_publication_verified") is not True:
        errors.append("canonical_publication_not_verified")
    if plan.get("private_lifecycle_write_performed") is not False:
        errors.append("settlement_already_marked_written")
    if plan.get("canonical_write_performed") is not False:
        errors.append("canonical_write_flag")
    transaction_id = plan.get("transaction_id")
    if not isinstance(transaction_id, str) or len(transaction_id) != 20:
        errors.append("transaction_id")
    items = plan.get("items")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        errors.append("settlement_items")
        return [], sorted(set(errors))

    normalized: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    seen_destinations: set[str] = set()
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            errors.append(f"item_{index}_invalid")
            continue
        candidate_id = raw.get("candidate_id")
        candidate_sha = raw.get("candidate_sha256")
        if not isinstance(candidate_id, str) or len(candidate_id) != 20:
            errors.append(f"item_{index}_candidate_id")
        if not isinstance(candidate_sha, str) or len(candidate_sha) != 64:
            errors.append(f"item_{index}_candidate_sha")
        try:
            source = safe_candidate_relative(raw.get("source_path"), "READY_FOR_CURATION/")
            destination = safe_candidate_relative(raw.get("destination_path"), "RESOLVED/PUBLISHED/")
        except SettlementFailure as exc:
            errors.append(f"item_{index}_{exc}")
            continue
        if candidate_id in seen_candidates:
            errors.append(f"item_{index}_duplicate_candidate")
        if destination in seen_destinations:
            errors.append(f"item_{index}_duplicate_destination")
        if isinstance(candidate_id, str):
            seen_candidates.add(candidate_id)
        seen_destinations.add(destination)
        normalized.append({**raw, "source_path": source, "destination_path": destination})
    return normalized, sorted(set(errors))


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def apply_settlement(
    root: Path,
    plan: dict[str, Any] | None,
    *,
    fail_after_moves: int | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    root = root.resolve()
    try:
        require_fixture(root)
    except SettlementFailure as exc:
        return None, [str(exc)]
    items, errors = validate_plan(plan)
    if errors or not isinstance(plan, dict):
        return None, errors or ["settlement_plan_invalid"]

    candidate_root = root / CANDIDATE_ROOT
    moves = 0
    recovered = 0
    try:
        for index, item in enumerate(items):
            source = candidate_root / item["source_path"]
            destination = candidate_root / item["destination_path"]
            expected_sha = item["candidate_sha256"]
            source_bytes = _read(source)
            destination_bytes = _read(destination)

            if source_bytes is None:
                if destination_bytes is not None and sha256(destination_bytes) == expected_sha:
                    recovered += 1
                    continue
                raise SettlementFailure(f"item_{index}_candidate_missing")
            if sha256(source_bytes) != expected_sha:
                raise SettlementFailure(f"item_{index}_source_sha_changed")
            if destination_bytes is not None:
                if sha256(destination_bytes) != expected_sha:
                    raise SettlementFailure(f"item_{index}_destination_conflict")
                source.unlink()
                recovered += 1
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)
            if _read(destination) != source_bytes:
                raise SettlementFailure(f"item_{index}_destination_verify_failed")
            moves += 1
            if fail_after_moves is not None and moves >= fail_after_moves:
                raise SettlementFailure("injected_failure_after_move")

        state = {
            "schema_version": SCHEMA_VERSION,
            "settlement_fixture_version": SETTLEMENT_FIXTURE_VERSION,
            "settlement_plan_version": SETTLEMENT_PLAN_VERSION,
            "transaction_id": plan.get("transaction_id"),
            "state": "SETTLED_FIXTURE",
            "canonical_publication_verified": True,
            "candidates": len(items),
            "moves": moves,
            "recovered_existing_destinations": recovered,
            "private_lifecycle_write_performed": True,
            "canonical_write_performed": False,
            "items": items,
        }
        state_path = candidate_root / "SETTLEMENTS" / f"{plan['transaction_id']}.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return state, []
    except (OSError, SettlementFailure) as exc:
        code = str(exc) if isinstance(exc, SettlementFailure) else "settlement_fixture_io_failed"
        return {
            "schema_version": SCHEMA_VERSION,
            "settlement_fixture_version": SETTLEMENT_FIXTURE_VERSION,
            "transaction_id": plan.get("transaction_id"),
            "state": "PARTIAL_RECOVERABLE",
            "moves_before_failure": moves,
            "private_lifecycle_write_performed": moves > 0,
            "canonical_write_performed": False,
        }, [code]


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply candidate settlement only inside an explicit local fixture.")
    parser.add_argument("--root-dir", type=Path, required=True)
    parser.add_argument("--settlement-plan", type=Path, required=True)
    parser.add_argument("--fail-after-moves", type=int)
    parser.add_argument("--report-out", type=Path)
    args = parser.parse_args()

    plan = read_json(args.settlement_plan)
    report, errors = apply_settlement(
        args.root_dir,
        plan,
        fail_after_moves=args.fail_after_moves,
    )
    if args.report_out and report is not None:
        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        args.report_out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if errors:
        state = report.get("state") if isinstance(report, dict) else "FAILED"
        print(
            "candidate_settlement_fixture_error "
            f"state={state} codes={','.join(errors)} canonical_write=0"
        )
        return 2
    assert report is not None
    print(
        "candidate_settlement_fixture_ok "
        f"state={report['state']} candidates={report['candidates']} moves={report['moves']} "
        f"recovered={report['recovered_existing_destinations']} canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
