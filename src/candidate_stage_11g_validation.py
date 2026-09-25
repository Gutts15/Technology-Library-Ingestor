#!/usr/bin/env python3
"""Aggregate Stage 11G validation into one operator command.

Runs the local recovery engine smoke, the real-rclone private restart fixture, the
authorized real-batch publisher plan, verifies the live production coordinator is
FREE, and confirms the production publisher still exposes no --apply-canonical path.

This validator itself never writes 00_LIBRARY.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from candidate_production_coordinator import read_state

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str]) -> tuple[bool, str]:
    result = subprocess.run(
        cmd,
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the consolidated Stage 11G recovery validation.")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--git-remote-url", required=True)
    parser.add_argument("--readiness", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "candidate-stage-11g-validation.json")
    args = parser.parse_args()

    failures: list[str] = []
    outputs: list[str] = []

    ok, out = run([sys.executable, str(ROOT / "tests/test_candidate_production_recovery_engine.py")])
    outputs.append(out)
    if not ok:
        failures.append("local_recovery_engine")

    fixture_out = ROOT / "candidate-production-restart-recovery-fixture.json"
    ok, out = run(
        [
            sys.executable,
            str(ROOT / "src/candidate_production_restart_recovery_fixture.py"),
            "--remote",
            str(args.remote),
            "--apply-remote-fixture",
            "--out",
            str(fixture_out),
        ]
    )
    outputs.append(out)
    if not ok:
        failures.append("remote_restart_fixture")

    ok, out = run(
        [
            sys.executable,
            str(ROOT / "src/candidate_transaction_publish.py"),
            "--remote",
            str(args.remote),
            "--readiness",
            str(args.readiness),
            "--authorization",
            str(args.authorization),
        ]
    )
    outputs.append(out)
    if not ok:
        failures.append("real_batch_plan")

    coordinator_sha, coordinator, coordinator_errors = read_state(str(args.git_remote_url))
    coordinator_free = (
        not coordinator_errors
        and isinstance(coordinator_sha, str)
        and bool(coordinator_sha)
        and isinstance(coordinator, dict)
        and coordinator.get("state") == "FREE"
    )
    if not coordinator_free:
        failures.append("production_coordinator_not_free")

    publisher_source = (ROOT / "src/candidate_transaction_publish.py").read_text(encoding="utf-8")
    apply_disabled = "--apply-canonical" not in publisher_source
    if not apply_disabled:
        failures.append("canonical_apply_unexpectedly_enabled")

    import json
    report = {
        "state": "PASS" if not failures else "FAILED",
        "stage": "11G",
        "local_recovery_engine": "local_recovery_engine" not in failures,
        "remote_restart_fixture": "remote_restart_fixture" not in failures,
        "real_batch_plan": "real_batch_plan" not in failures,
        "production_coordinator_free": coordinator_free,
        "canonical_apply_disabled": apply_disabled,
        "failures": failures,
        "outputs": outputs,
        "canonical_write_performed": False,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if failures:
        print(
            "candidate_stage_11g_validation_error "
            f"codes={','.join(failures)} canonical_write=0"
        )
        return 2

    print(
        "candidate_stage_11g_validation_ok state=PASS local_recovery=1 remote_restart=1 "
        "pending_block=1 rollback=1 recovered_release=1 post_recovery_acquire=1 "
        "real_batch_plan=1 production_coordinator_free=1 apply_canonical=0 canonical_write=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
