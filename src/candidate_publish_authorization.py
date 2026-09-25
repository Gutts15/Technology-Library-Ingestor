#!/usr/bin/env python3
"""Record explicit local operator authorization for one readiness-verified transaction.

This module is the deliberate boundary between technical readiness and permission to
perform canonical writes. It never contacts remote storage and never writes
``00_LIBRARY``. Plan mode only validates the readiness artifact. Record mode requires
three exact confirmations supplied by the operator and writes a local authorization
artifact bound to the exact readiness bytes, batch id and transaction id.

An authorization artifact is necessary but not sufficient for canonical publication:
a future production executor must still revalidate live state immediately before any
write and must require its own explicit ``--apply-canonical`` operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from candidate_executor_readiness import READY_STATE

SCHEMA_VERSION = 1
AUTHORIZATION_VERSION = "0.1.0"
AUTHORIZATION_STATE = "OPERATOR_AUTHORIZED_FOR_CANONICAL_APPLY"
CONFIRM_PHRASE = "AUTHORIZE_CANONICAL_WRITE"
ID_RE = re.compile(r"^[0-9a-f]{20}$")
EXPECTED_CHECKS = {
    "live_batch_preconditions",
    "editorial_acceptance_bound",
    "structural_retrieval_preflight",
    "semantic_routing_quality",
    "transaction_plan_live_recomputed",
    "remote_coordination_capability",
    "remote_recovery_capability",
    "remote_success_path_fixture",
    "remote_restart_recovery_fixture",
}


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def parse_readiness(raw: bytes | None) -> tuple[dict[str, Any] | None, list[str]]:
    if raw is None:
        return None, ["readiness_missing"]
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, ["readiness_invalid_json"]
    if not isinstance(payload, dict):
        return None, ["readiness_invalid"]

    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("readiness_schema")
    if payload.get("state") != READY_STATE:
        errors.append("readiness_state")
    batch_id = payload.get("batch_id")
    transaction_id = payload.get("transaction_id")
    if not isinstance(batch_id, str) or not ID_RE.fullmatch(batch_id):
        errors.append("readiness_batch_id")
    if not isinstance(transaction_id, str) or not ID_RE.fullmatch(transaction_id):
        errors.append("readiness_transaction_id")
    checks = payload.get("checks")
    if not isinstance(checks, dict) or set(checks) != EXPECTED_CHECKS:
        errors.append("readiness_checks")
    elif any(checks.get(name) is not True for name in EXPECTED_CHECKS):
        errors.append("readiness_checks_not_all_true")
    if payload.get("production_publish_authorized") is not False:
        errors.append("readiness_authorization_flag")
    if payload.get("canonical_write_performed") is not False:
        errors.append("readiness_write_flag")
    return (payload if not errors else None), sorted(set(errors))


def build_authorization(
    readiness_raw: bytes,
    *,
    confirm_batch: str,
    confirm_transaction: str,
    confirm_phrase: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    readiness, errors = parse_readiness(readiness_raw)
    if errors or readiness is None:
        return None, errors or ["readiness_invalid"]

    expected_batch = str(readiness["batch_id"])
    expected_transaction = str(readiness["transaction_id"])
    confirmations: list[str] = []
    if confirm_batch != expected_batch:
        confirmations.append("confirmation_batch_mismatch")
    if confirm_transaction != expected_transaction:
        confirmations.append("confirmation_transaction_mismatch")
    if confirm_phrase != CONFIRM_PHRASE:
        confirmations.append("confirmation_phrase_mismatch")
    if confirmations:
        return None, sorted(set(confirmations))

    return {
        "schema_version": SCHEMA_VERSION,
        "authorization_version": AUTHORIZATION_VERSION,
        "state": AUTHORIZATION_STATE,
        "readiness_sha256": sha256(readiness_raw),
        "readiness_state": READY_STATE,
        "batch_id": expected_batch,
        "transaction_id": expected_transaction,
        "operator_confirmation_explicit": True,
        "confirmation_phrase_id": CONFIRM_PHRASE,
        "production_publish_authorized": True,
        "canonical_write_performed": False,
    }, []


def validate_authorization(
    payload: dict[str, Any] | None,
    readiness_raw: bytes,
) -> list[str]:
    readiness, readiness_errors = parse_readiness(readiness_raw)
    if readiness_errors or readiness is None:
        return [f"readiness:{code}" for code in readiness_errors or ["invalid"]]
    if not isinstance(payload, dict):
        return ["authorization_invalid"]

    expected = {
        "schema_version": SCHEMA_VERSION,
        "authorization_version": AUTHORIZATION_VERSION,
        "state": AUTHORIZATION_STATE,
        "readiness_sha256": sha256(readiness_raw),
        "readiness_state": READY_STATE,
        "batch_id": readiness["batch_id"],
        "transaction_id": readiness["transaction_id"],
        "operator_confirmation_explicit": True,
        "confirmation_phrase_id": CONFIRM_PHRASE,
        "production_publish_authorized": True,
        "canonical_write_performed": False,
    }
    errors: list[str] = []
    for key, value in expected.items():
        if payload.get(key) != value:
            errors.append(f"authorization_{key}")
    return sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record explicit local operator authorization bound to one executor-readiness artifact."
    )
    parser.add_argument("--readiness", type=Path, required=True)
    parser.add_argument("--authorize-current-readiness", action="store_true")
    parser.add_argument("--confirm-batch")
    parser.add_argument("--confirm-transaction")
    parser.add_argument("--confirm-phrase")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    raw = read_bytes(args.readiness)
    readiness, errors = parse_readiness(raw)
    if errors or readiness is None or raw is None:
        print(
            "candidate_publish_authorization_error "
            f"codes={','.join(errors or ['readiness_invalid'])} canonical_write=0"
        )
        return 2

    if not args.authorize_current_readiness:
        print(
            "candidate_publish_authorization_ok mode=plan "
            f"batch_id={readiness['batch_id']} transaction_id={readiness['transaction_id']} "
            "authorized=0 canonical_write=0"
        )
        return 0

    if args.out is None:
        print("candidate_publish_authorization_error code=out_required canonical_write=0")
        return 2
    if not all(isinstance(value, str) for value in (args.confirm_batch, args.confirm_transaction, args.confirm_phrase)):
        print("candidate_publish_authorization_error code=explicit_confirmation_required canonical_write=0")
        return 2

    authorization, auth_errors = build_authorization(
        raw,
        confirm_batch=str(args.confirm_batch),
        confirm_transaction=str(args.confirm_transaction),
        confirm_phrase=str(args.confirm_phrase),
    )
    if auth_errors or authorization is None:
        print(
            "candidate_publish_authorization_error "
            f"codes={','.join(auth_errors or ['authorization_failed'])} canonical_write=0"
        )
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(authorization, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_publish_authorization_ok mode=record_local "
        f"batch_id={authorization['batch_id']} transaction_id={authorization['transaction_id']} "
        "authorized=1 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
