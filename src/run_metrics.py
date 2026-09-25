#!/usr/bin/env python3
"""Aggregate privacy-safe ingest telemetry across recent workflow runs.

The input reports are already sanitized by run_report.py, but this module still
uses a strict allow-list and never copies run IDs, package IDs, filenames,
provider-specific paths, URLs or arbitrary fields into the aggregate output.

It supports two modes:
- local: aggregate JSON reports from a local directory;
- remote: list/read a bounded recent window from an rclone backend and
  optionally persist one compact latest metrics document.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import json
import math
import shutil
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

METRICS_VERSION = "0.1.0"
SCHEMA_VERSION = 1
MAX_METRICS_BYTES = 24 * 1024
DEFAULT_SOURCE = "99_INBOX/PROCESS_LOG/RUNS"
DEFAULT_DESTINATION = "99_INBOX/PROCESS_LOG/METRICS/latest.json"
DEFAULT_MAX_RUNS = 50
MAX_MAX_RUNS = 200
PIPELINES = ("links", "images", "audio", "videos", "content", "spreadsheets")
STATUSES = ("success", "failure", "cancelled", "unknown")
HEAVY_CHOICES = ("audio", "video", "none", "unknown")
DEPENDENCIES = (
    "storage_rclone",
    "ffmpeg",
    "tesseract",
    "poppler",
    "transcription_python",
    "spreadsheet_python",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_int(value: Any, default: int = 0, maximum: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    number = max(0, number)
    if maximum is not None:
        number = min(number, maximum)
    return number


def read_json_text(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return read_json_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sanitize_pipeline(value: Any) -> dict[str, int]:
    payload = value if isinstance(value, dict) else {}
    return {
        "planned": safe_int(payload.get("planned")),
        "selected": safe_int(payload.get("selected")),
        "processed": safe_int(payload.get("processed")),
        "finalized": safe_int(payload.get("finalized")),
        "rejected": safe_int(payload.get("rejected")),
        "errors": safe_int(payload.get("errors")),
        "warnings": safe_int(payload.get("warnings")),
    }


def sanitize_report(report: dict[str, Any]) -> dict[str, Any] | None:
    if safe_int(report.get("schema_version")) != 1:
        return None

    run = report.get("run") if isinstance(report.get("run"), dict) else {}
    plan = report.get("plan") if isinstance(report.get("plan"), dict) else {}
    totals = report.get("totals") if isinstance(report.get("totals"), dict) else {}
    dependencies = report.get("dependencies") if isinstance(report.get("dependencies"), dict) else {}
    pipelines = report.get("pipelines") if isinstance(report.get("pipelines"), dict) else {}

    finished = parse_iso(run.get("finished_at"))
    started = parse_iso(run.get("started_at"))
    if finished is None or started is None or finished < started:
        return None

    duration = safe_int((finished - started).total_seconds(), maximum=24 * 60 * 60)
    status = run.get("status") if run.get("status") in STATUSES else "unknown"

    heavy = plan.get("heavy_choice") if plan.get("heavy_choice") in HEAVY_CHOICES else "unknown"
    plan_totals = plan.get("totals") if isinstance(plan.get("totals"), dict) else {}

    sanitized_dependencies = {
        key: bool(dependencies.get(key)) if isinstance(dependencies.get(key), bool) else False
        for key in DEPENDENCIES
    }

    sanitized_pipelines = {
        key: sanitize_pipeline(pipelines.get(key))
        for key in PIPELINES
    }

    return {
        "started": started,
        "finished": finished,
        "duration_seconds": duration,
        "status": status,
        "heavy_choice": heavy,
        "plan": {
            "raw_pending_files": safe_int(plan_totals.get("raw_pending_files")),
            "supported_pending_files": safe_int(plan_totals.get("supported_pending_files")),
            "preserved_unsupported_files": safe_int(plan_totals.get("preserved_unsupported_files")),
            "pending_bytes": safe_int(plan_totals.get("pending_bytes")),
            "planned_files": safe_int(plan_totals.get("planned_files")),
        },
        "dependencies": sanitized_dependencies,
        "pipelines": sanitized_pipelines,
        "totals": {
            "planned": safe_int(totals.get("planned")),
            "selected": safe_int(totals.get("selected")),
            "processed": safe_int(totals.get("processed")),
            "finalized": safe_int(totals.get("finalized")),
            "rejected": safe_int(totals.get("rejected")),
            "errors": safe_int(totals.get("errors")),
            "warnings": safe_int(totals.get("warnings")),
        },
    }


def percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def rounded_ratio(numerator: int | float, denominator: int | float) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator), 4)


def aggregate_reports(reports: list[dict[str, Any]], *, seen: int, invalid: int, max_runs: int) -> dict[str, Any]:
    reports = sorted(reports, key=lambda item: item["finished"])
    durations = [item["duration_seconds"] for item in reports]
    duration_sum = sum(durations)
    duration_mean = round(duration_sum / len(durations), 2) if durations else 0.0
    duration_median = round(float(statistics.median(durations)), 2) if durations else 0.0

    status_counts = {key: 0 for key in STATUSES}
    heavy_counts = {key: 0 for key in HEAVY_CHOICES}
    dependency_counts = {key: 0 for key in DEPENDENCIES}
    pipeline_totals = {
        key: {
            "planned": 0,
            "selected": 0,
            "processed": 0,
            "finalized": 0,
            "rejected": 0,
            "errors": 0,
            "warnings": 0,
        }
        for key in PIPELINES
    }
    workload_totals = {
        "planned": 0,
        "selected": 0,
        "processed": 0,
        "finalized": 0,
        "rejected": 0,
        "errors": 0,
        "warnings": 0,
    }

    backlog_keys = (
        "raw_pending_files",
        "supported_pending_files",
        "preserved_unsupported_files",
        "pending_bytes",
        "planned_files",
    )
    backlog_series = {key: [] for key in backlog_keys}

    work_runs = 0
    empty_plans = 0
    runs_with_errors = 0

    for report in reports:
        status_counts[report["status"]] += 1
        heavy_counts[report["heavy_choice"]] += 1
        for key in DEPENDENCIES:
            if report["dependencies"].get(key):
                dependency_counts[key] += 1

        for key in backlog_keys:
            backlog_series[key].append(report["plan"][key])

        if report["totals"]["planned"] > 0:
            work_runs += 1
        else:
            empty_plans += 1
        if report["totals"]["errors"] > 0 or report["status"] == "failure":
            runs_with_errors += 1

        for field in workload_totals:
            workload_totals[field] += report["totals"][field]
        for pipeline in PIPELINES:
            for field in pipeline_totals[pipeline]:
                pipeline_totals[pipeline][field] += report["pipelines"][pipeline][field]

    latest = reports[-1] if reports else None
    earliest = reports[0] if reports else None
    handled = workload_totals["processed"] + workload_totals["finalized"] + workload_totals["rejected"]
    observed_minutes = round(duration_sum / 60.0, 2)
    throughput_per_minute = round(handled / (duration_sum / 60.0), 4) if duration_sum > 0 else 0.0

    backlog: dict[str, Any] = {}
    for key, values in backlog_series.items():
        backlog[key] = {
            "mean": round(sum(values) / len(values), 2) if values else 0.0,
            "max": max(values) if values else 0,
            "latest": latest["plan"][key] if latest else 0,
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "metrics_version": METRICS_VERSION,
        "generated_at": utc_now(),
        "window": {
            "reports_seen": seen,
            "reports_used": len(reports),
            "invalid_reports": invalid,
            "max_runs": max_runs,
            "started_at": earliest["started"].isoformat() if earliest else None,
            "finished_at": latest["finished"].isoformat() if latest else None,
        },
        "runs": {
            "total": len(reports),
            "status": status_counts,
            "work_runs": work_runs,
            "empty_plans": empty_plans,
            "runs_with_errors": runs_with_errors,
            "work_run_rate": rounded_ratio(work_runs, len(reports)),
            "error_run_rate": rounded_ratio(runs_with_errors, len(reports)),
            "duration_seconds": {
                "sum": duration_sum,
                "mean": duration_mean,
                "median": duration_median,
                "p90": percentile(durations, 0.90),
                "max": max(durations) if durations else 0,
            },
            "observed_runtime_minutes": observed_minutes,
        },
        "workload": {
            **workload_totals,
            "handled": handled,
            "selection_ratio": rounded_ratio(workload_totals["selected"], workload_totals["planned"]),
            "handled_ratio": rounded_ratio(handled, workload_totals["selected"]),
            "handled_per_observed_minute": throughput_per_minute,
        },
        "backlog_at_plan": backlog,
        "heavy_choice": heavy_counts,
        "dependencies_runs": dependency_counts,
        "pipelines": pipeline_totals,
    }


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def remote_entries(remote: str, source: str) -> list[dict[str, Any]] | None:
    if not shutil.which("rclone"):
        return None
    result = run_rclone([
        "lsjson",
        join_remote(remote, source),
        "--files-only",
        "--max-depth",
        "1",
        "--log-level",
        "ERROR",
    ])
    if result.returncode != 0:
        return None
    try:
        values = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(values, list):
        return None
    return [item for item in values if isinstance(item, dict)]


def remote_report_payload(remote: str, source: str, name: str) -> dict[str, Any] | None:
    result = run_rclone([
        "cat",
        join_remote(remote, f"{source.strip('/')}/{name}"),
        "--log-level",
        "ERROR",
    ])
    if result.returncode != 0:
        return None
    return read_json_text(result.stdout)


def load_remote_reports(remote: str, source: str, max_runs: int) -> tuple[list[dict[str, Any]], int, int] | None:
    entries = remote_entries(remote, source)
    if entries is None:
        return None

    candidates: list[tuple[datetime, str]] = []
    for item in entries:
        name = item.get("Name") or item.get("Path")
        mod_time = parse_iso(item.get("ModTime"))
        if (
            not isinstance(name, str)
            or not name.startswith("run-")
            or not name.endswith(".json")
            or len(name) > 160
            or "/" in name
            or "\\" in name
            or mod_time is None
        ):
            continue
        candidates.append((mod_time, name))

    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = candidates[:max_runs]
    sanitized: list[dict[str, Any]] = []
    invalid = 0
    for _, name in selected:
        payload = remote_report_payload(remote, source, name)
        safe = sanitize_report(payload) if payload is not None else None
        if safe is None:
            invalid += 1
            continue
        sanitized.append(safe)
    return sanitized, len(candidates), invalid


def load_local_reports(source_dir: Path, max_runs: int) -> tuple[list[dict[str, Any]], int, int]:
    candidates = [
        path
        for path in source_dir.glob("run-*.json")
        if path.is_file()
    ]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    selected = candidates[:max_runs]
    sanitized: list[dict[str, Any]] = []
    invalid = 0
    for path in selected:
        payload = read_json_file(path)
        safe = sanitize_report(payload) if payload is not None else None
        if safe is None:
            invalid += 1
            continue
        sanitized.append(safe)
    return sanitized, len(candidates), invalid


def encoded_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


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
    parser = argparse.ArgumentParser(description="Aggregate recent privacy-safe Technology Library ingest run telemetry.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--source-dir", type=Path)
    mode.add_argument("--remote")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--destination", default=DEFAULT_DESTINATION)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-runs", type=int, default=DEFAULT_MAX_RUNS)
    args = parser.parse_args()

    if not 1 <= args.max_runs <= MAX_MAX_RUNS:
        parser.error(f"--max-runs must be between 1 and {MAX_MAX_RUNS}")

    if args.source_dir is not None:
        reports, seen, invalid = load_local_reports(args.source_dir.resolve(), args.max_runs)
    else:
        loaded = load_remote_reports(args.remote, args.source, args.max_runs)
        if loaded is None:
            print("run_metrics_error code=remote_read_failed")
            return 2
        reports, seen, invalid = loaded

    metrics = aggregate_reports(reports, seen=seen, invalid=invalid, max_runs=args.max_runs)
    if encoded_size(metrics) > MAX_METRICS_BYTES:
        print("run_metrics_error code=metrics_budget_exceeded")
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    uploaded = False
    if args.remote:
        uploaded = persist_remote(args.remote, args.destination, args.out)
        if not uploaded:
            print("run_metrics_warning code=upload_failed")
            return 2

    print(
        "run_metrics_ok "
        f"seen={seen} used={len(reports)} invalid={invalid} "
        f"work_runs={metrics['runs']['work_runs']} observed_minutes={metrics['runs']['observed_runtime_minutes']} "
        f"handled={metrics['workload']['handled']} uploaded={1 if uploaded else 0}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
