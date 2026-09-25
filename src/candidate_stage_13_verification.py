#!/usr/bin/env python3
"""Stage 13 post-publication canonical inventory + deterministic index verification.

Read-only against 00_LIBRARY. It independently enumerates canonical raw records,
validates every record, rebuilds the expected generated indexes in memory, and
requires byte-for-byte equality with the live MASTER/domain INDEX files.

This stage does not rewrite canonical files. The controlled publisher already
performed the rebuild inside the committed transaction; Stage 13 proves that rebuild
is complete and idempotent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from candidate_remote_recovery_probe import join_remote, remote_cat, run_rclone
from library_index_build import RECORD_NAME_RE, planned_indexes, validate_record

SCHEMA_VERSION = 1
STAGE13_VERSION = "0.1.0"
MASTER = "00_LIBRARY/MASTER_INDEX.md"
MASTER_COUNT_RE = re.compile(rb"(?m)^RECORDS:\s*(\d+)\s*$")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def strict_record_paths(remote: str) -> tuple[list[str], list[str]]:
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
    return sorted(paths), []


def load_validated_records(remote: str, paths: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    records: list[dict[str, str]] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    seen_titles: set[tuple[str, str]] = set()

    for path in paths:
        raw = remote_cat(remote, path)
        if raw is None:
            errors.append(f"record_missing:{path}")
            continue
        try:
            record = validate_record(path, raw)
        except ValueError as exc:
            errors.append(f"{exc}:{path}")
            continue
        rid = record["record_id"]
        title_key = (record["record_type"], record["title"].casefold())
        if rid in seen_ids:
            errors.append(f"duplicate_record_id:{rid}")
        if title_key in seen_titles:
            errors.append(f"duplicate_canonical_title:{record['record_type']}:{record['title']}")
        seen_ids.add(rid)
        seen_titles.add(title_key)
        records.append(record)

    records.sort(key=lambda r: (r["domain"], r["category"], r["record_type"], r["title"].casefold()))
    return records, sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Stage 13 canonical inventory and generated indexes.")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--publication-report", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("candidate-stage-13-verification.json"))
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("candidate_stage_13_verification_error code=rclone_missing canonical_write=0")
        return 2

    try:
        publication = json.loads(args.publication_report.read_text(encoding="utf-8"))
        receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        print("candidate_stage_13_verification_error code=publication_evidence_read_failed canonical_write=0")
        return 2
    if not isinstance(publication, dict) or not isinstance(receipt, dict):
        print("candidate_stage_13_verification_error code=publication_evidence_invalid canonical_write=0")
        return 2

    errors: list[str] = []
    if publication.get("state") != "COMMITTED":
        errors.append("publication_not_committed")
    if publication.get("canonical_write_performed") is not True:
        errors.append("publication_write_flag")
    if receipt.get("state") != "VERIFIED_COMMITTED_STATE":
        errors.append("receipt_state")
    if receipt.get("transaction_id") != publication.get("transaction_id"):
        errors.append("receipt_transaction_mismatch")

    paths, path_errors = strict_record_paths(str(args.remote))
    errors.extend(path_errors)
    records, record_errors = load_validated_records(str(args.remote), paths)
    errors.extend(record_errors)

    master = remote_cat(str(args.remote), MASTER)
    if master is None:
        errors.append("master_missing")
        master_count = None
    else:
        match = MASTER_COUNT_RE.search(master)
        master_count = int(match.group(1)) if match else None
        if master_count is None:
            errors.append("master_count_missing")
        elif master_count != len(records):
            errors.append(f"master_count_mismatch:{master_count}:{len(records)}")

    expected_after = publication.get("records_after")
    if not isinstance(expected_after, int) or expected_after != len(records):
        errors.append(f"publication_record_count_mismatch:{expected_after}:{len(records)}")

    expected_indexes = planned_indexes(records) if records else {}
    index_mismatches: list[str] = []
    for path, expected in sorted(expected_indexes.items()):
        actual = remote_cat(str(args.remote), path)
        if actual != expected:
            index_mismatches.append(path)
    errors.extend(f"index_bytes_mismatch:{path}" for path in index_mismatches)

    if master is not None and MASTER in expected_indexes and master != expected_indexes[MASTER]:
        errors.append("master_not_deterministic")

    if errors:
        print(
            "candidate_stage_13_verification_error "
            f"codes={','.join(sorted(set(errors)))} canonical_write=0"
        )
        return 3

    domains = sorted({record["domain"] for record in records})
    report = {
        "schema_version": SCHEMA_VERSION,
        "stage13_version": STAGE13_VERSION,
        "state": "PASS",
        "transaction_id": publication.get("transaction_id"),
        "batch_id": publication.get("batch_id"),
        "records": len(records),
        "master_records": master_count,
        "domains": domains,
        "indexes": len(expected_indexes),
        "index_mismatches": [],
        "master_sha256": sha256(master or b""),
        "publication_receipt_sha256": publication.get("receipt_sha256"),
        "inventory_verified": True,
        "deterministic_rebuild_verified": True,
        "index_bytes_verified": True,
        "canonical_write_performed": False,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "candidate_stage_13_verification_ok state=PASS "
        f"records={report['records']} master_records={report['master_records']} "
        f"domains={len(domains)} indexes={report['indexes']} "
        "inventory_verified=1 deterministic_rebuild=1 index_bytes_verified=1 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
