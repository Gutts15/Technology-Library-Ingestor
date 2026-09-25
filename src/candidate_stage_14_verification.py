#!/usr/bin/env python3
"""Stage 14 consolidated post-publication verification.

Read-only against canonical 00_LIBRARY. The stage independently verifies:
14A exact target bytes/SHA for every receipt item;
14B canonical structural integrity + index references;
14C structural retrieval regression + fresh-request semantic routing on the live catalog;
14D one settlement-gating acceptance report.

No canonical or candidate lifecycle writes are performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_retrieval_preflight import assess_cases
from candidate_retrieval_routing_local import (
    DEFAULT_ENDPOINT,
    build_virtual_entries,
    endpoint_is_loopback,
    read_cases,
    run_benchmark,
)
from candidate_remote_recovery_probe import remote_cat
from candidate_stage_13_verification import load_validated_records, strict_record_paths
from library_index_build import validate_record

SCHEMA_VERSION = 1
STAGE14_VERSION = "0.2.0"
MASTER = "00_LIBRARY/MASTER_INDEX.md"


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def target_index_path(target: str) -> str | None:
    parts = PurePosixPath(target).parts
    if len(parts) < 3 or parts[0] != "00_LIBRARY":
        return None
    if parts[1] == "SOURCES":
        return "00_LIBRARY/SOURCES/INDEX.md"
    return f"00_LIBRARY/{parts[1]}/INDEX.md"


def build_failure_report(
    publication: dict[str, Any],
    receipt: dict[str, Any],
    records: list[dict[str, Any]],
    verified_targets: list[dict[str, Any]],
    structural_cases: list[dict[str, Any]],
    semantic_report: dict[str, Any] | None,
    errors: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "stage14_version": STAGE14_VERSION,
        "state": "FAIL",
        "batch_id": publication.get("batch_id"),
        "transaction_id": receipt.get("transaction_id"),
        "records": len(records),
        "receipt_targets": len(verified_targets),
        "verified_targets": verified_targets,
        "structural_retrieval_cases": structural_cases,
        "semantic_routing": semantic_report,
        "errors": sorted(set(errors)),
        "post_publication_accepted": False,
        "candidate_settlement_allowed": False,
        "canonical_write_performed": False,
    }


def verify_receipt_targets(
    remote: str,
    receipt: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    items = receipt.get("items")
    if not isinstance(items, list):
        return [], ["receipt_items_invalid"]

    verified: list[dict[str, Any]] = []
    errors: list[str] = []
    master = remote_cat(remote, MASTER)
    if master is None:
        return [], ["master_missing"]
    try:
        master_text = master.decode("utf-8")
    except UnicodeDecodeError:
        return [], ["master_utf8"]

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"receipt_item_{index}_invalid")
            continue
        target = item.get("target_path")
        expected_sha = item.get("content_sha256")
        record_id = item.get("record_id")
        action = item.get("action")
        if action not in {"CREATE", "UPDATE", "UNCHANGED"}:
            errors.append(f"receipt_item_{index}_action")
        if not isinstance(target, str) or not target.startswith("00_LIBRARY/"):
            errors.append(f"receipt_item_{index}_target")
            continue
        if not isinstance(expected_sha, str) or len(expected_sha) != 64:
            errors.append(f"receipt_item_{index}_sha")
            continue
        if not isinstance(record_id, str):
            errors.append(f"receipt_item_{index}_record_id")
            continue

        raw = remote_cat(remote, target)
        if raw is None:
            errors.append(f"target_missing:{target}")
            continue
        actual_sha = sha256(raw)
        if actual_sha != expected_sha:
            errors.append(f"target_sha_mismatch:{target}")
            continue

        try:
            record = validate_record(target, raw)
        except ValueError as exc:
            errors.append(f"target_record_invalid:{exc}:{target}")
            continue
        if record["record_id"] != record_id:
            errors.append(f"target_record_id_mismatch:{target}")

        if target not in master_text:
            errors.append(f"master_reference_missing:{target}")

        index_path = target_index_path(target)
        if index_path is None:
            errors.append(f"target_index_path_invalid:{target}")
            continue
        index_raw = remote_cat(remote, index_path)
        if index_raw is None:
            errors.append(f"domain_index_missing:{index_path}")
            continue
        try:
            index_text = index_raw.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"domain_index_utf8:{index_path}")
            continue
        if Path(target).name not in index_text:
            errors.append(f"domain_index_reference_missing:{target}")

        verified.append(
            {
                "action": action,
                "record_id": record_id,
                "target_path": target,
                "content_sha256": actual_sha,
                "title": record["title"],
                "record_type": record["record_type"],
                "domain": record["domain"],
                "category": record["category"],
                "index_path": index_path,
            }
        )

    return verified, sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run consolidated Stage 14 post-publication verification.")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--publication-report", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--stage13-report", type=Path, required=True)
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tests" / "retrieval_regression_cases.json",
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", type=Path, default=Path("candidate-stage-14-verification.json"))
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("candidate_stage_14_verification_error code=rclone_missing canonical_write=0")
        return 2
    if not endpoint_is_loopback(args.endpoint):
        print("candidate_stage_14_verification_error code=non_loopback_model_endpoint canonical_write=0")
        return 2

    publication = read_json(args.publication_report)
    receipt = read_json(args.receipt)
    stage13 = read_json(args.stage13_report)
    cases_payload = read_cases(args.cases)
    if not all(isinstance(value, dict) for value in (publication, receipt, stage13, cases_payload)):
        print("candidate_stage_14_verification_error code=evidence_read_failed canonical_write=0")
        return 2

    assert publication is not None
    assert receipt is not None
    assert stage13 is not None
    assert cases_payload is not None

    errors: list[str] = []
    if publication.get("state") != "COMMITTED":
        errors.append("publication_not_committed")
    if receipt.get("state") != "VERIFIED_COMMITTED_STATE":
        errors.append("receipt_state")
    if stage13.get("state") != "PASS":
        errors.append("stage13_state")
    if stage13.get("inventory_verified") is not True:
        errors.append("stage13_inventory")
    if stage13.get("index_bytes_verified") is not True:
        errors.append("stage13_indexes")
    if publication.get("transaction_id") != receipt.get("transaction_id"):
        errors.append("transaction_receipt_mismatch")
    if stage13.get("transaction_id") != receipt.get("transaction_id"):
        errors.append("transaction_stage13_mismatch")

    verified_targets, target_errors = verify_receipt_targets(str(args.remote), receipt)
    errors.extend(target_errors)

    paths, path_errors = strict_record_paths(str(args.remote))
    errors.extend(path_errors)
    records, record_errors = load_validated_records(str(args.remote), paths)
    errors.extend(record_errors)

    expected_records = publication.get("records_after")
    if not isinstance(expected_records, int) or len(records) != expected_records:
        errors.append(f"record_count_mismatch:{len(records)}:{expected_records}")

    structural_cases, structural_errors = assess_cases(records, cases_payload)
    errors.extend(structural_errors)
    structural_failed = [
        row.get("id") for row in structural_cases if isinstance(row, dict) and row.get("state") != "PASS"
    ]
    if structural_failed:
        errors.append("structural_retrieval_failed:" + ",".join(str(value) for value in structural_failed))

    reader = lambda relative: remote_cat(str(args.remote), relative)
    entries, entry_errors = build_virtual_entries(paths, {"items": []}, reader)
    errors.extend(entry_errors)

    semantic_report: dict[str, Any] | None = None
    if not errors:
        semantic_report, semantic_errors = run_benchmark(
            cases_payload,
            entries,
            model=args.model,
            endpoint=args.endpoint,
            timeout=max(10.0, min(float(args.timeout), 600.0)),
        )
        errors.extend(semantic_errors)
        if semantic_report is None:
            errors.append("semantic_report_missing")
        elif semantic_report.get("state") != "PASS" or semantic_report.get("routing_quality_proven") is not True:
            failed = semantic_report.get("failed_case_ids")
            errors.append("semantic_routing_failed:" + ",".join(str(value) for value in (failed or [])))

    if errors:
        failure_report = build_failure_report(
            publication,
            receipt,
            records,
            verified_targets,
            structural_cases,
            semantic_report,
            errors,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(failure_report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            "candidate_stage_14_verification_error "
            f"codes={','.join(failure_report['errors'])} diagnostic_report=1 canonical_write=0"
        )
        return 3

    assert semantic_report is not None
    report = {
        "schema_version": SCHEMA_VERSION,
        "stage14_version": STAGE14_VERSION,
        "state": "PASS",
        "batch_id": publication.get("batch_id"),
        "transaction_id": receipt.get("transaction_id"),
        "records": len(records),
        "receipt_targets": len(verified_targets),
        "verified_targets": verified_targets,
        "target_bytes_verified": True,
        "target_sha256_verified": True,
        "structural_integrity_verified": True,
        "index_references_verified": True,
        "structural_retrieval_cases": structural_cases,
        "structural_retrieval_verified": True,
        "semantic_routing": semantic_report,
        "semantic_routing_verified": True,
        "post_publication_accepted": True,
        "candidate_settlement_allowed": True,
        "canonical_write_performed": False,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "candidate_stage_14_verification_ok state=PASS "
        f"records={report['records']} targets={report['receipt_targets']} "
        f"structural_cases={len(structural_cases)} semantic_cases={len(semantic_report.get('cases') or [])} "
        "target_bytes=1 target_sha256=1 structural_integrity=1 index_references=1 "
        "structural_retrieval=1 semantic_routing=1 settlement_allowed=1 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
