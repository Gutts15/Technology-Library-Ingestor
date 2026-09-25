#!/usr/bin/env python3
"""Stage 12A publication preflight for the exact authorized candidate transaction.

This is a read-only gate. It proves that Stage 11 is complete, the live sealed
batch/transaction still match readiness + authorization, the production coordinator
is FREE, and no non-terminal production recovery journal remains for the transaction.

It also binds the exact local executor module bytes into the preflight report.
There is deliberately no canonical write path in this module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from candidate_canonical_transaction_engine import TRANSACTION_ENGINE_VERSION
from candidate_production_coordinator import COORDINATOR_VERSION, read_state
from candidate_production_recovery_engine import RECOVERY_ENGINE_VERSION
from candidate_publish_authorization import parse_readiness, validate_authorization
from candidate_remote_recovery_probe import join_remote, remote_json
from candidate_transaction_commit_engine import COMMIT_ENGINE_VERSION
from candidate_transaction_prewrite_arm import PREWRITE_ARM_VERSION, RECOVERY_ROOT, load_live_context

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
PREFLIGHT_VERSION = "0.1.0"
READY_STATE = "READY_FOR_CONTROLLED_PUBLICATION"
TERMINAL_RECOVERY_STATES = {"ABORTED_PREWRITE", "RECOVERED", "COMMITTED"}

EXECUTOR_MODULES = {
    "candidate_canonical_transaction_engine.py": ROOT / "src/candidate_canonical_transaction_engine.py",
    "candidate_production_coordinator.py": ROOT / "src/candidate_production_coordinator.py",
    "candidate_production_recovery_engine.py": ROOT / "src/candidate_production_recovery_engine.py",
    "candidate_transaction_commit_engine.py": ROOT / "src/candidate_transaction_commit_engine.py",
    "candidate_transaction_prewrite_arm.py": ROOT / "src/candidate_transaction_prewrite_arm.py",
}


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def validate_stage11g(report: dict[str, Any] | None) -> list[str]:
    if not isinstance(report, dict):
        return ["stage11g_report_invalid"]
    errors: list[str] = []
    expected_true = {
        "local_recovery_engine",
        "remote_restart_fixture",
        "real_batch_plan",
        "production_coordinator_free",
        "canonical_apply_disabled",
    }
    if report.get("state") != "PASS":
        errors.append("stage11g_state")
    if report.get("stage") != "11G":
        errors.append("stage11g_stage")
    for field in sorted(expected_true):
        if report.get(field) is not True:
            errors.append(f"stage11g_missing:{field}")
    if report.get("failures") != []:
        errors.append("stage11g_failures")
    if report.get("canonical_write_performed") is not False:
        errors.append("stage11g_write_flag")
    return sorted(set(errors))


def run_rclone(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def transaction_recovery_state(remote: str, transaction_id: str) -> tuple[list[dict[str, Any]], list[str]]:
    result = run_rclone(
        [
            "lsjson",
            join_remote(remote, RECOVERY_ROOT),
            "--recursive",
            "--files-only",
            "--log-level",
            "ERROR",
        ]
    )
    if result.returncode != 0:
        return [], ["recovery_root_list_failed"]
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return [], ["recovery_root_list_invalid_json"]
    if not isinstance(rows, list):
        return [], ["recovery_root_list_invalid"]

    prefix = transaction_id + "/"
    journals: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rel = row.get("Path")
        if not isinstance(rel, str):
            continue
        rel = rel.replace("\\", "/").strip("/")
        if not rel.startswith(prefix) or not rel.endswith("/journal.json"):
            continue
        logical = RECOVERY_ROOT.rstrip("/") + "/" + rel
        payload = remote_json(remote, logical)
        if not isinstance(payload, dict):
            errors.append(f"recovery_journal_invalid:{rel}")
            continue
        if payload.get("transaction_id") != transaction_id:
            errors.append(f"recovery_journal_transaction_mismatch:{rel}")
            continue
        state = payload.get("state")
        journals.append(
            {
                "path": logical,
                "state": state,
                "owner": payload.get("owner"),
                "canonical_write_performed": payload.get("canonical_write_performed"),
            }
        )
        if state not in TERMINAL_RECOVERY_STATES:
            errors.append(f"pending_recovery:{rel}:{state}")
    return sorted(journals, key=lambda x: str(x["path"])), sorted(set(errors))


def executor_fingerprints() -> tuple[dict[str, str], list[str]]:
    output: dict[str, str] = {}
    errors: list[str] = []
    for name, path in EXECUTOR_MODULES.items():
        try:
            raw = path.read_bytes()
        except OSError:
            errors.append(f"executor_module_missing:{name}")
            continue
        output[name] = sha256(raw)
    return output, sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run read-only Stage 12A controlled-publication preflight.")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--git-remote-url", required=True)
    parser.add_argument("--readiness", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--stage11g-report", type=Path, required=True)
    parser.add_argument("--root", default="99_INBOX/CANDIDATES")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if not shutil.which("rclone") or not shutil.which("git"):
        print("candidate_stage_12a_preflight_error code=dependency_missing canonical_write=0")
        return 2

    errors = validate_stage11g(read_json(args.stage11g_report))

    try:
        readiness_raw = args.readiness.read_bytes()
        authorization_raw = args.authorization.read_bytes()
        authorization = json.loads(authorization_raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        print("candidate_stage_12a_preflight_error code=local_evidence_read_failed canonical_write=0")
        return 2
    if not isinstance(authorization, dict):
        print("candidate_stage_12a_preflight_error code=authorization_invalid canonical_write=0")
        return 2

    readiness, readiness_errors = parse_readiness(readiness_raw)
    errors.extend(f"readiness:{code}" for code in readiness_errors)
    if readiness is None:
        print(
            "candidate_stage_12a_preflight_error "
            f"codes={','.join(sorted(set(errors or ['readiness_invalid'])))} canonical_write=0"
        )
        return 3

    errors.extend(validate_authorization(authorization, readiness_raw))

    context, live_errors = load_live_context(
        remote=str(args.remote),
        root_base=args.root.strip("/"),
        readiness_raw=readiness_raw,
        authorization=authorization,
    )
    errors.extend(f"live:{code}" for code in live_errors)
    if context is None:
        print(
            "candidate_stage_12a_preflight_error "
            f"codes={','.join(sorted(set(errors or ['live_context_failed'])))} canonical_write=0"
        )
        return 3

    transaction = context["transaction"]
    if transaction.get("transaction_id") != readiness.get("transaction_id"):
        errors.append("transaction_readiness_mismatch")
    if transaction.get("finalize_batch_id") != readiness.get("batch_id"):
        errors.append("batch_readiness_mismatch")

    _, coordinator, coordinator_errors = read_state(str(args.git_remote_url))
    errors.extend(f"coordinator:{code}" for code in coordinator_errors)
    coordinator_free = (
        not coordinator_errors
        and isinstance(coordinator, dict)
        and coordinator.get("state") == "FREE"
    )
    if not coordinator_free:
        errors.append("coordinator_not_free")

    journals, recovery_errors = transaction_recovery_state(
        str(args.remote),
        str(readiness["transaction_id"]),
    )
    errors.extend(recovery_errors)

    fingerprints, fingerprint_errors = executor_fingerprints()
    errors.extend(fingerprint_errors)

    if errors:
        print(
            "candidate_stage_12a_preflight_error "
            f"codes={','.join(sorted(set(errors)))} canonical_write=0"
        )
        return 4

    report = {
        "schema_version": SCHEMA_VERSION,
        "preflight_version": PREFLIGHT_VERSION,
        "state": READY_STATE,
        "batch_id": readiness["batch_id"],
        "transaction_id": readiness["transaction_id"],
        "readiness_sha256": sha256(readiness_raw),
        "authorization_sha256": sha256(authorization_raw),
        "stage11g_report_sha256": sha256(args.stage11g_report.read_bytes()),
        "transaction_plan_sha256": sha256(context["transaction_raw"]),
        "master_index_before_sha256": transaction.get("master_index_sha256"),
        "coordinator_free": True,
        "pending_recovery": False,
        "terminal_recovery_journals": journals,
        "executor_versions": {
            "transaction_engine": TRANSACTION_ENGINE_VERSION,
            "commit_engine": COMMIT_ENGINE_VERSION,
            "recovery_engine": RECOVERY_ENGINE_VERSION,
            "coordinator": COORDINATOR_VERSION,
            "prewrite_arm": PREWRITE_ARM_VERSION,
        },
        "executor_source_sha256": fingerprints,
        "authorization_verified": True,
        "live_transaction_revalidated": True,
        "canonical_apply_enabled": False,
        "canonical_write_performed": False,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "candidate_stage_12a_preflight_ok state=READY_FOR_CONTROLLED_PUBLICATION "
        f"batch_id={report['batch_id']} transaction_id={report['transaction_id']} "
        f"terminal_recovery_journals={len(journals)} coordinator_free=1 "
        "authorization=1 live_revalidated=1 apply_canonical=0 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
