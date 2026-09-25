#!/usr/bin/env python3
"""Assess whether ingest telemetry is mature enough for cadence review.

This module does not create a schedule and does not recommend an exact cron.
It applies a conservative, explicit readiness policy to the sanitized aggregate
metrics so zero-touch scheduling is considered only after representative and
stable real workload data exists.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import json
import math
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ASSESSMENT_VERSION = "0.1.0"
SCHEMA_VERSION = 1
MAX_ASSESSMENT_BYTES = 8 * 1024
DEFAULT_DESTINATION = "99_INBOX/PROCESS_LOG/METRICS/cadence-readiness.json"

MIN_TOTAL_RUNS = 10
MIN_WORK_RUNS = 5
MIN_HEAVY_RUNS = 3
MAX_INVALID_REPORT_RATE = 0.10
MAX_ERROR_RUN_RATE = 0.20
MAX_P90_DURATION_SECONDS = 25 * 60
HIGH_EMPTY_RUN_RATE = 0.50
MODERATE_EMPTY_RUN_RATE = 0.25


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return max(0.0, number)


def read_metrics(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if safe_int(payload.get("schema_version")) != 1:
        return None
    return payload


def extract_signals(metrics: dict[str, Any]) -> dict[str, Any]:
    window = metrics.get("window") if isinstance(metrics.get("window"), dict) else {}
    runs = metrics.get("runs") if isinstance(metrics.get("runs"), dict) else {}
    durations = runs.get("duration_seconds") if isinstance(runs.get("duration_seconds"), dict) else {}
    heavy = metrics.get("heavy_choice") if isinstance(metrics.get("heavy_choice"), dict) else {}
    backlog = metrics.get("backlog_at_plan") if isinstance(metrics.get("backlog_at_plan"), dict) else {}
    supported = backlog.get("supported_pending_files") if isinstance(backlog.get("supported_pending_files"), dict) else {}

    total_runs = safe_int(runs.get("total"))
    work_runs = safe_int(runs.get("work_runs"))
    empty_runs = safe_int(runs.get("empty_plans"))
    reports_seen = safe_int(window.get("reports_seen"))
    invalid_reports = safe_int(window.get("invalid_reports"))
    heavy_runs = safe_int(heavy.get("audio")) + safe_int(heavy.get("video"))

    invalid_rate = round(invalid_reports / reports_seen, 4) if reports_seen else 0.0
    empty_rate = round(empty_runs / total_runs, 4) if total_runs else 0.0
    error_rate = safe_float(runs.get("error_run_rate"))

    return {
        "total_runs": total_runs,
        "work_runs": work_runs,
        "heavy_runs": heavy_runs,
        "reports_seen": reports_seen,
        "invalid_reports": invalid_reports,
        "invalid_report_rate": invalid_rate,
        "error_run_rate": round(error_rate, 4),
        "empty_run_rate": empty_rate,
        "p90_duration_seconds": safe_int(durations.get("p90")),
        "observed_runtime_minutes": round(safe_float(runs.get("observed_runtime_minutes")), 2),
        "latest_supported_pending_files": safe_int(supported.get("latest")),
    }


def empty_run_signal(rate: float) -> str:
    if rate >= HIGH_EMPTY_RUN_RATE:
        return "high"
    if rate >= MODERATE_EMPTY_RUN_RATE:
        return "moderate"
    return "low"


def build_assessment(metrics: dict[str, Any]) -> dict[str, Any]:
    signals = extract_signals(metrics)

    insufficient: list[str] = []
    if signals["total_runs"] < MIN_TOTAL_RUNS:
        insufficient.append("need_more_total_runs")
    if signals["work_runs"] < MIN_WORK_RUNS:
        insufficient.append("need_more_work_runs")
    if signals["heavy_runs"] < MIN_HEAVY_RUNS:
        insufficient.append("need_more_heavy_runs")

    blockers: list[str] = []
    if signals["invalid_report_rate"] > MAX_INVALID_REPORT_RATE:
        blockers.append("too_many_invalid_reports")
    if signals["error_run_rate"] > MAX_ERROR_RUN_RATE:
        blockers.append("error_rate_too_high")
    if signals["p90_duration_seconds"] > MAX_P90_DURATION_SECONDS:
        blockers.append("p90_too_close_to_job_timeout")

    if insufficient:
        state = "insufficient_data"
        ready = False
    elif blockers:
        state = "hold"
        ready = False
    else:
        state = "ready_for_cadence_review"
        ready = True

    return {
        "schema_version": SCHEMA_VERSION,
        "assessment_version": ASSESSMENT_VERSION,
        "generated_at": utc_now(),
        "state": state,
        "ready": ready,
        "policy": {
            "minimum_total_runs": MIN_TOTAL_RUNS,
            "minimum_work_runs": MIN_WORK_RUNS,
            "minimum_heavy_runs": MIN_HEAVY_RUNS,
            "maximum_invalid_report_rate": MAX_INVALID_REPORT_RATE,
            "maximum_error_run_rate": MAX_ERROR_RUN_RATE,
            "maximum_p90_duration_seconds": MAX_P90_DURATION_SECONDS,
            "job_timeout_seconds": 30 * 60,
            "exact_schedule_recommendation": False,
        },
        "signals": {
            **signals,
            "empty_run_waste_signal": empty_run_signal(signals["empty_run_rate"]),
            "backlog_present": signals["latest_supported_pending_files"] > 0,
        },
        "insufficient_data_reasons": insufficient,
        "stability_blockers": blockers,
    }


def encoded_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def persist_remote(remote: str, destination: str, local_path: Path) -> bool:
    if not shutil.which("rclone"):
        return False
    parent = destination.rsplit("/", 1)[0] if "/" in destination else ""
    if parent:
        if run_rclone(["mkdir", join_remote(remote, parent), "--log-level", "ERROR"]).returncode != 0:
            return False
    return run_rclone([
        "copyto",
        str(local_path),
        join_remote(remote, destination),
        "--log-level",
        "ERROR",
        "--stats",
        "0",
    ]).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Assess whether sanitized ingest telemetry is ready for cadence review.")
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--remote")
    parser.add_argument("--destination", default=DEFAULT_DESTINATION)
    args = parser.parse_args()

    metrics = read_metrics(args.metrics.resolve())
    if metrics is None:
        print("cadence_assess_error code=metrics_invalid_or_missing")
        return 2

    assessment = build_assessment(metrics)
    if encoded_size(assessment) > MAX_ASSESSMENT_BYTES:
        print("cadence_assess_error code=assessment_budget_exceeded")
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(assessment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    uploaded = False
    if args.remote:
        uploaded = persist_remote(args.remote, args.destination, args.out)
        if not uploaded:
            print("cadence_assess_warning code=upload_failed")
            return 2

    print(
        "cadence_assess_ok "
        f"state={assessment['state']} ready={1 if assessment['ready'] else 0} "
        f"total_runs={assessment['signals']['total_runs']} work_runs={assessment['signals']['work_runs']} "
        f"heavy_runs={assessment['signals']['heavy_runs']} uploaded={1 if uploaded else 0}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
