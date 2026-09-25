#!/usr/bin/env python3
"""Probe durable private recovery material on the configured rclone provider.

The probe is intentionally unable to publish candidate records. It writes only
synthetic fixture data below ``99_INBOX/CANDIDATES/RECOVERY_REMOTE_FIXTURE``.
It never reads or writes ``00_LIBRARY`` and never authorizes publication.

Apply mode proves that recovery material can be persisted and read back exactly,
then simulates an interrupted transaction by mutating a disposable target. A fresh
Python process reopens the remote journal/snapshot, restores the target byte-for-byte,
marks the journal RECOVERED, and the parent verifies the terminal state before
cleaning the disposable fixture tree.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

PROBE_VERSION = "0.1.0"
SCHEMA_VERSION = 1
FIXTURE_ROOT = "99_INBOX/CANDIDATES/RECOVERY_REMOTE_FIXTURE"
SNAPSHOT_NAME = "snapshot-before.bin"
TARGET_NAME = "synthetic-target.bin"
JOURNAL_NAME = "journal.json"
RESULT_NAME = "resume-result.json"

BEFORE_BYTES = b"technology-library-recovery-fixture-before\n"
AFTER_BYTES = b"technology-library-recovery-fixture-mutated\n"


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def safe_fixture_id(value: str) -> bool:
    return len(value) == 24 and all(ch in "0123456789abcdef" for ch in value)


def fixture_prefix(fixture_id: str) -> str:
    if not safe_fixture_id(fixture_id):
        raise ValueError("unsafe_fixture_id")
    return str(PurePosixPath(FIXTURE_ROOT) / fixture_id)


def run_rclone(args: list[str], *, binary: bool = False) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["rclone", *args],
        check=False,
        capture_output=True,
        text=not binary,
        env=os.environ.copy(),
    )


def remote_cat(remote_root: str, relative: str) -> bytes | None:
    result = run_rclone(["cat", join_remote(remote_root, relative), "--log-level", "ERROR"], binary=True)
    if result.returncode != 0:
        return None
    return bytes(result.stdout)


def remote_copy_bytes(remote_root: str, relative: str, raw: bytes) -> bool:
    with tempfile.TemporaryDirectory(prefix="tl-recovery-upload-") as temp:
        local = Path(temp) / "payload.bin"
        local.write_bytes(raw)
        result = run_rclone(
            ["copyto", str(local), join_remote(remote_root, relative), "--log-level", "ERROR", "--stats", "0"]
        )
        return result.returncode == 0


def remote_copy_json(remote_root: str, relative: str, payload: dict[str, Any]) -> bool:
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return remote_copy_bytes(remote_root, relative, raw)


def remote_json(remote_root: str, relative: str) -> dict[str, Any] | None:
    raw = remote_cat(remote_root, relative)
    if raw is None:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def cleanup_fixture(remote_root: str, fixture_id: str) -> bool:
    try:
        prefix = fixture_prefix(fixture_id)
    except ValueError:
        return False
    purge = run_rclone(["purge", join_remote(remote_root, prefix), "--log-level", "ERROR"])
    if purge.returncode != 0:
        return False
    check = run_rclone(["lsjson", join_remote(remote_root, prefix), "--log-level", "ERROR"])
    if check.returncode != 0:
        return True
    try:
        listed = json.loads(check.stdout or "[]")
    except json.JSONDecodeError:
        return False
    return listed == []


def build_prepared_journal(fixture_id: str) -> dict[str, Any]:
    prefix = fixture_prefix(fixture_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "probe_version": PROBE_VERSION,
        "fixture_only": True,
        "fixture_id": fixture_id,
        "state": "PREPARED",
        "snapshot_path": str(PurePosixPath(prefix) / SNAPSHOT_NAME),
        "target_path": str(PurePosixPath(prefix) / TARGET_NAME),
        "snapshot_sha256": sha256(BEFORE_BYTES),
        "target_before_sha256": sha256(BEFORE_BYTES),
        "rollback_verified": False,
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }


def validate_prepared_journal(payload: dict[str, Any] | None, fixture_id: str) -> list[str]:
    if not isinstance(payload, dict):
        return ["journal_missing_or_invalid"]
    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("journal_schema")
    if payload.get("probe_version") != PROBE_VERSION:
        errors.append("journal_probe_version")
    if payload.get("fixture_only") is not True:
        errors.append("journal_fixture_flag")
    if payload.get("fixture_id") != fixture_id:
        errors.append("journal_fixture_id")
    if payload.get("state") != "PREPARED":
        errors.append("journal_state")
    if payload.get("snapshot_sha256") != sha256(BEFORE_BYTES):
        errors.append("journal_snapshot_sha")
    if payload.get("target_before_sha256") != sha256(BEFORE_BYTES):
        errors.append("journal_target_before_sha")
    if payload.get("rollback_verified") is not False:
        errors.append("journal_rollback_flag")
    if payload.get("production_publish_authorized") is not False:
        errors.append("journal_authorization_flag")
    if payload.get("canonical_write_performed") is not False:
        errors.append("journal_write_flag")
    return sorted(set(errors))


def resume_recovery(remote_root: str, fixture_id: str, result_path: Path | None) -> int:
    """Fresh-process recovery path used by the parent integration probe."""
    if not safe_fixture_id(fixture_id):
        print("candidate_remote_recovery_probe_error mode=resume code=unsafe_fixture_id canonical_write=0")
        return 2
    prefix = fixture_prefix(fixture_id)
    journal_rel = str(PurePosixPath(prefix) / JOURNAL_NAME)
    journal = remote_json(remote_root, journal_rel)
    errors = validate_prepared_journal(journal, fixture_id)
    snapshot = remote_cat(remote_root, str(PurePosixPath(prefix) / SNAPSHOT_NAME))
    current_target = remote_cat(remote_root, str(PurePosixPath(prefix) / TARGET_NAME))
    if snapshot is None or sha256(snapshot) != sha256(BEFORE_BYTES):
        errors.append("snapshot_unreadable_or_changed")
    if current_target is None or sha256(current_target) != sha256(AFTER_BYTES):
        errors.append("mutated_target_not_observed")
    if errors:
        print(
            "candidate_remote_recovery_probe_error mode=resume "
            f"codes={','.join(sorted(set(errors)))} canonical_write=0"
        )
        return 2

    target_rel = str(PurePosixPath(prefix) / TARGET_NAME)
    if not remote_copy_bytes(remote_root, target_rel, snapshot):
        print("candidate_remote_recovery_probe_error mode=resume code=restore_write_failed canonical_write=0")
        return 2
    restored = remote_cat(remote_root, target_rel)
    if restored != snapshot or restored != BEFORE_BYTES:
        print("candidate_remote_recovery_probe_error mode=resume code=restore_verify_failed canonical_write=0")
        return 2

    recovered = dict(journal or {})
    recovered["state"] = "RECOVERED"
    recovered["rollback_verified"] = True
    recovered["restored_target_sha256"] = sha256(restored)
    if not remote_copy_json(remote_root, journal_rel, recovered):
        print("candidate_remote_recovery_probe_error mode=resume code=recovered_journal_write_failed canonical_write=0")
        return 2
    reread = remote_json(remote_root, journal_rel)
    if reread != recovered:
        print("candidate_remote_recovery_probe_error mode=resume code=recovered_journal_verify_failed canonical_write=0")
        return 2

    result = {
        "state": "RECOVERED",
        "fixture_id": fixture_id,
        "rollback_verified": True,
        "restored_target_sha256": sha256(restored),
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }
    if result_path is not None:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_remote_recovery_probe_resume_ok state=RECOVERED rollback_verified=1 "
        "authorized=0 canonical_write=0"
    )
    return 0


def apply_probe(remote_root: str, out: Path | None) -> int:
    if not shutil.which("rclone"):
        print("candidate_remote_recovery_probe_error code=rclone_missing canonical_write=0")
        return 2

    fixture_id = uuid.uuid4().hex[:24]
    prefix = fixture_prefix(fixture_id)
    snapshot_rel = str(PurePosixPath(prefix) / SNAPSHOT_NAME)
    target_rel = str(PurePosixPath(prefix) / TARGET_NAME)
    journal_rel = str(PurePosixPath(prefix) / JOURNAL_NAME)
    cleanup_required = True
    errors: list[str] = []
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "probe_version": PROBE_VERSION,
        "state": "FAILED",
        "fixture_id": fixture_id,
        "fixture_path": prefix,
        "journal_persisted": False,
        "snapshot_verified": False,
        "restart_recovery_verified": False,
        "rollback_verified": False,
        "cleanup_verified": False,
        "cleanup_required": True,
        "production_publish_authorized": False,
        "canonical_write_performed": False,
    }

    journal = build_prepared_journal(fixture_id)
    if not remote_copy_bytes(remote_root, snapshot_rel, BEFORE_BYTES):
        errors.append("snapshot_write_failed")
    if not errors and not remote_copy_bytes(remote_root, target_rel, BEFORE_BYTES):
        errors.append("target_seed_failed")
    if not errors and not remote_copy_json(remote_root, journal_rel, journal):
        errors.append("journal_write_failed")

    if not errors:
        snapshot = remote_cat(remote_root, snapshot_rel)
        target = remote_cat(remote_root, target_rel)
        reread_journal = remote_json(remote_root, journal_rel)
        report["journal_persisted"] = reread_journal == journal
        report["snapshot_verified"] = snapshot == BEFORE_BYTES and target == BEFORE_BYTES
        errors.extend(validate_prepared_journal(reread_journal, fixture_id))
        if not report["journal_persisted"]:
            errors.append("journal_readback_failed")
        if not report["snapshot_verified"]:
            errors.append("snapshot_readback_failed")

    if not errors and not remote_copy_bytes(remote_root, target_rel, AFTER_BYTES):
        errors.append("target_mutation_failed")
    if not errors and remote_cat(remote_root, target_rel) != AFTER_BYTES:
        errors.append("target_mutation_verify_failed")

    with tempfile.TemporaryDirectory(prefix="tl-recovery-resume-") as temp:
        result_path = Path(temp) / RESULT_NAME
        if not errors:
            child = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--remote",
                    remote_root,
                    "--resume-recovery",
                    fixture_id,
                    "--resume-result",
                    str(result_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=os.environ.copy(),
            )
            if child.returncode != 0:
                errors.append("fresh_process_recovery_failed")
            else:
                try:
                    child_result = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    child_result = None
                report["restart_recovery_verified"] = (
                    isinstance(child_result, dict)
                    and child_result.get("state") == "RECOVERED"
                    and child_result.get("rollback_verified") is True
                    and child_result.get("canonical_write_performed") is False
                )
                if not report["restart_recovery_verified"]:
                    errors.append("fresh_process_result_invalid")

    if not errors:
        restored = remote_cat(remote_root, target_rel)
        terminal_journal = remote_json(remote_root, journal_rel)
        report["rollback_verified"] = (
            restored == BEFORE_BYTES
            and isinstance(terminal_journal, dict)
            and terminal_journal.get("state") == "RECOVERED"
            and terminal_journal.get("rollback_verified") is True
            and terminal_journal.get("restored_target_sha256") == sha256(BEFORE_BYTES)
        )
        if not report["rollback_verified"]:
            errors.append("terminal_recovery_verify_failed")

    cleanup_ok = cleanup_fixture(remote_root, fixture_id)
    report["cleanup_verified"] = cleanup_ok
    report["cleanup_required"] = not cleanup_ok
    cleanup_required = not cleanup_ok
    if not cleanup_ok:
        errors.append("cleanup_failed")

    if not errors:
        report["state"] = "PASS"
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if errors:
        print(
            "candidate_remote_recovery_probe_error mode=apply_fixture "
            f"codes={','.join(sorted(set(errors)))} fixture_path={prefix} "
            f"cleanup_required={1 if cleanup_required else 0} authorized=0 canonical_write=0"
        )
        return 2

    print(
        "candidate_remote_recovery_probe_ok mode=apply_fixture state=PASS "
        "journal_persisted=1 snapshot_verified=1 restart_recovery=1 rollback_verified=1 "
        "cleanup_verified=1 authorized=0 canonical_write=0"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe private durable recovery through rclone without canonical writes.")
    parser.add_argument("--remote", required=True, help="rclone root, e.g. tl:")
    parser.add_argument("--apply-remote-fixture", action="store_true")
    parser.add_argument("--resume-recovery")
    parser.add_argument("--resume-result", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if args.resume_recovery is not None:
        return resume_recovery(args.remote, args.resume_recovery, args.resume_result)

    if not args.apply_remote_fixture:
        print(
            "candidate_remote_recovery_probe_ok mode=plan remote_write=0 "
            f"fixture_root={FIXTURE_ROOT} authorized=0 canonical_write=0"
        )
        return 0

    return apply_probe(args.remote, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
