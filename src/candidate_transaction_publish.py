#!/usr/bin/env python3
"""Validate the exact inputs for the future production candidate publisher.

This is the first implementation slice of the production executor. It is intentionally
plan-only: it validates the explicit operator authorization, re-inspects the live sealed
batch, recomputes the private transaction from live storage, and requires byte-for-byte
agreement with TRANSACTION_READY.

It has no canonical write path, does not acquire the production coordinator, does not
create a recovery journal, and cannot write 00_LIBRARY. Canonical apply will be added
only after this contract is proven against the real batch.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
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
from candidate_publish_authorization import parse_readiness, validate_authorization
from candidate_transaction_prepare import DEFAULT_TRANSACTION_READY, parse_update_manifest, prepare_transaction

PUBLISH_EXECUTOR_VERSION = "0.1.0"


def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def evaluate_publish_inputs(
    *,
    readiness_raw: bytes | None,
    authorization: dict[str, Any] | None,
    batch_report: dict[str, Any] | None,
    stored_transaction: dict[str, Any] | None,
    recomputed_transaction: dict[str, Any] | None,
) -> list[str]:
    errors: list[str] = []
    readiness, readiness_errors = parse_readiness(readiness_raw)
    errors.extend(f"readiness:{code}" for code in readiness_errors)
    if readiness is None or readiness_raw is None:
        return sorted(set(errors or ["readiness_invalid"]))

    errors.extend(validate_authorization(authorization, readiness_raw))

    if not isinstance(batch_report, dict):
        errors.append("batch_report_invalid")
    else:
        if batch_report.get("state") != "READY_FOR_EDITORIAL_REVIEW":
            errors.append("batch_state")
        if batch_report.get("live_preconditions_verified") is not True:
            errors.append("batch_live_preconditions")
        if batch_report.get("batch_id") != readiness.get("batch_id"):
            errors.append("batch_readiness_binding")
        if batch_report.get("canonical_write_performed") is not False:
            errors.append("batch_write_flag")

    if not isinstance(stored_transaction, dict) or not isinstance(recomputed_transaction, dict):
        errors.append("transaction_missing")
    else:
        if stored_transaction != recomputed_transaction:
            errors.append("transaction_live_recompute_mismatch")
        if stored_transaction.get("transaction_id") != readiness.get("transaction_id"):
            errors.append("transaction_readiness_binding")
        if stored_transaction.get("finalize_batch_id") != readiness.get("batch_id"):
            errors.append("transaction_batch_binding")
        if stored_transaction.get("live_preconditions_verified") is not True:
            errors.append("transaction_live_preconditions")
        if stored_transaction.get("canonical_write_performed") is not False:
            errors.append("transaction_write_flag")

    if isinstance(authorization, dict):
        if authorization.get("production_publish_authorized") is not True:
            errors.append("authorization_not_explicit")
        if authorization.get("canonical_write_performed") is not False:
            errors.append("authorization_write_flag")

    return sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate authorized live inputs for the future production candidate publisher."
    )
    parser.add_argument("--remote", required=True)
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--readiness", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("candidate_transaction_publish_error code=rclone_missing canonical_write=0")
        return 2

    try:
        readiness_raw = args.readiness.read_bytes()
    except OSError:
        print("candidate_transaction_publish_error code=readiness_missing canonical_write=0")
        return 2
    authorization = read_json_file(args.authorization)

    remote = str(args.remote)
    root_base = args.root.strip("/")
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
        print(
            "candidate_transaction_publish_error "
            f"codes={','.join(batch_errors or ['batch_inspection_failed'])} canonical_write=0"
        )
        return 2

    update_manifest = read_json_bytes(update_raw)
    updates, update_errors = parse_update_manifest(update_manifest)
    if update_errors:
        print(
            "candidate_transaction_publish_error "
            f"codes={','.join(update_errors)} canonical_write=0"
        )
        return 2

    recomputed_transaction, transaction_errors = prepare_transaction(
        master_content=master_raw,
        new_manifest=read_json_bytes(publish_raw),
        new_validation=read_json_bytes(validation_raw),
        update_artifacts=updates,
        read_content=reader,
        finalize_batch_id=str(batch_report.get("batch_id")),
    )
    if transaction_errors or recomputed_transaction is None:
        print(
            "candidate_transaction_publish_error "
            f"codes={','.join(transaction_errors or ['transaction_recompute_failed'])} canonical_write=0"
        )
        return 2

    stored_transaction = read_json_bytes(transaction_raw)
    errors = evaluate_publish_inputs(
        readiness_raw=readiness_raw,
        authorization=authorization,
        batch_report=batch_report,
        stored_transaction=stored_transaction,
        recomputed_transaction=recomputed_transaction,
    )
    if errors:
        print(
            "candidate_transaction_publish_error "
            f"codes={','.join(errors)} authorized=0 canonical_write=0"
        )
        return 3

    readiness, _ = parse_readiness(readiness_raw)
    assert readiness is not None
    counts = stored_transaction.get("counts") if isinstance(stored_transaction, dict) else {}
    print(
        "candidate_transaction_publish_ok "
        f"mode=plan version={PUBLISH_EXECUTOR_VERSION} "
        f"batch_id={readiness['batch_id']} transaction_id={readiness['transaction_id']} "
        f"writes={counts.get('writes')} create={counts.get('create')} "
        f"update={counts.get('update')} unchanged={counts.get('unchanged')} "
        "inputs_verified=1 authorized=1 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
