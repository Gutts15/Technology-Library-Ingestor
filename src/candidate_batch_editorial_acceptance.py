#!/usr/bin/env python3
"""Create a local machine-readable editorial acceptance bound to one sealed candidate batch.

This helper does not review content by itself. It records an explicit operator attestation
that the currently inspected batch has already been reviewed and accepted. The artifact
is bound to the exact batch id, MASTER_INDEX SHA and current item content SHAs so later
readiness checks fail closed if the batch changes.

The acceptance file is local-only. This module never writes remote storage, never writes
00_LIBRARY and never authorizes publication.
"""

from __future__ import annotations

import argparse
import json
import shutil
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
    read_local,
    read_remote,
)

SCHEMA_VERSION = 1
ACCEPTANCE_VERSION = "0.1.0"


def _bindings(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in report.get("items", []):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "title": item.get("title"),
                "record_id": item.get("record_id"),
                "action": item.get("action"),
                "target_path": item.get("target_path"),
                "content_sha256": item.get("content_sha256"),
            }
        )
    return sorted(rows, key=lambda row: (str(row.get("target_path")), str(row.get("record_id"))))


def build_acceptance(report: dict[str, Any] | None) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(report, dict):
        return None, ["batch_report_invalid"]
    errors: list[str] = []
    if report.get("state") != "READY_FOR_EDITORIAL_REVIEW":
        errors.append("batch_not_ready_for_editorial_review")
    if report.get("live_preconditions_verified") is not True:
        errors.append("batch_live_preconditions_not_verified")
    if report.get("canonical_write_performed") is not False:
        errors.append("batch_write_flag")
    batch_id = report.get("batch_id")
    master_sha = report.get("master_index_sha256")
    items = _bindings(report)
    if not isinstance(batch_id, str) or not batch_id:
        errors.append("batch_id")
    if not isinstance(master_sha, str) or len(master_sha) != 64:
        errors.append("master_index_sha256")
    if not items:
        errors.append("batch_items_empty")
    for index, item in enumerate(items):
        if not all(isinstance(item.get(key), str) and item.get(key) for key in ("title", "record_id", "action", "target_path", "content_sha256")):
            errors.append(f"item_{index}_binding")
    if errors:
        return None, sorted(set(errors))

    counts = report.get("counts") if isinstance(report.get("counts"), dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "acceptance_version": ACCEPTANCE_VERSION,
        "state": "EDITORIALLY_ACCEPTED",
        "batch_id": batch_id,
        "master_index_sha256": master_sha,
        "counts": {
            "items": counts.get("items"),
            "create": counts.get("create"),
            "update": counts.get("update"),
            "unchanged": counts.get("unchanged"),
        },
        "items": items,
        "editorial_review_complete": True,
        "all_current_items_accepted": True,
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }, []


def validate_acceptance(payload: dict[str, Any] | None, report: dict[str, Any] | None) -> list[str]:
    if not isinstance(payload, dict):
        return ["acceptance_invalid"]
    expected, errors = build_acceptance(report)
    if errors or expected is None:
        return [f"batch:{code}" for code in errors or ["invalid"]]
    checks = {
        "schema_version": SCHEMA_VERSION,
        "acceptance_version": ACCEPTANCE_VERSION,
        "state": "EDITORIALLY_ACCEPTED",
        "batch_id": expected["batch_id"],
        "master_index_sha256": expected["master_index_sha256"],
        "editorial_review_complete": True,
        "all_current_items_accepted": True,
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }
    output: list[str] = []
    for key, value in checks.items():
        if payload.get(key) != value:
            output.append(f"acceptance_{key}")
    if payload.get("counts") != expected.get("counts"):
        output.append("acceptance_counts")
    if payload.get("items") != expected.get("items"):
        output.append("acceptance_item_bindings")
    return sorted(set(output))


def main() -> int:
    parser = argparse.ArgumentParser(description="Record explicit editorial acceptance for the exact current candidate batch.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root-dir", type=Path)
    mode.add_argument("--remote")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--accept-current-batch", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    root_base = args.root.strip("/")
    publish_rel = str(PurePosixPath(root_base) / DEFAULT_PUBLISH)
    validation_rel = str(PurePosixPath(root_base) / DEFAULT_VALIDATION)
    update_rel = str(PurePosixPath(root_base) / DEFAULT_UPDATE)
    batch_rel = str(PurePosixPath(root_base) / DEFAULT_BATCH)

    if args.root_dir is not None:
        root = args.root_dir.resolve()
        reader = lambda relative: read_local(root, relative)
    else:
        if not shutil.which("rclone"):
            print("candidate_batch_editorial_acceptance_error code=rclone_missing canonical_write=0")
            return 2
        remote = str(args.remote)
        reader = lambda relative: read_remote(remote, relative)

    report, batch_errors = inspect_batch(
        master_raw=reader(DEFAULT_MASTER),
        publish_raw=reader(publish_rel),
        validation_raw=reader(validation_rel),
        update_raw=reader(update_rel),
        batch_raw=reader(batch_rel),
        read_content=reader,
    )
    if batch_errors or report is None:
        print(
            "candidate_batch_editorial_acceptance_error "
            f"codes={','.join(batch_errors or ['batch_inspection_failed'])} canonical_write=0"
        )
        return 2

    acceptance, errors = build_acceptance(report)
    if errors or acceptance is None:
        print(
            "candidate_batch_editorial_acceptance_error "
            f"codes={','.join(errors or ['acceptance_build_failed'])} canonical_write=0"
        )
        return 2

    if not args.accept_current_batch:
        print(
            "candidate_batch_editorial_acceptance_ok mode=plan "
            f"batch_id={acceptance['batch_id']} items={acceptance['counts']['items']} "
            "accepted=0 authorized=0 canonical_write=0"
        )
        return 0

    if args.out is None:
        print("candidate_batch_editorial_acceptance_error code=out_required canonical_write=0")
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(acceptance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_batch_editorial_acceptance_ok mode=record_local "
        f"batch_id={acceptance['batch_id']} items={acceptance['counts']['items']} "
        "accepted=1 authorized=0 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
