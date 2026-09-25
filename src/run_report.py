#!/usr/bin/env python3
"""Build and optionally persist a privacy-safe ingest run report.

The report consumes only the already-sanitized workload plan plus aggregate
bridge summaries. Detailed bridge result arrays and package IDs are never copied
into run telemetry. This gives quota/runtime tuning data without turning
observability into a second source of private content.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPORT_VERSION = "0.1.0"
SCHEMA_VERSION = 1
MAX_REPORT_BYTES = 16 * 1024
DEFAULT_DESTINATION = "99_INBOX/PROCESS_LOG/RUNS"
SAFE_PROVIDER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

BRIDGE_SUMMARIES = {
    "links": ("tl-link-ingestor", "bridge-summary.json"),
    "images": ("tl-image-ingestor", "bridge-summary.json"),
    "audio": ("tl-audio-ingestor", "bridge-summary.json"),
    "videos": ("tl-video-ingestor", "bridge-summary.json"),
    "content": ("tl-content-ingestor", "content-bridge-summary.json"),
    "spreadsheets": ("tl-spreadsheet-ingestor", "spreadsheet-bridge-summary.json"),
}

BATCH_KEYS = {
    "links": "links",
    "images": "images",
    "audio": "audio",
    "videos": "videos",
    "content": "content",
    "spreadsheets": "spreadsheets",
}


def utc_iso(epoch: int | float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, number)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return payload if isinstance(payload, dict) else None


def derive_result_count(payload: dict[str, Any], status: str) -> int:
    results = payload.get("results")
    if not isinstance(results, list):
        return 0
    return sum(
        1
        for item in results
        if isinstance(item, dict) and item.get("status") == status
    )


def aggregate_bridge(path: Path, planned: int) -> dict[str, Any]:
    payload = read_json(path)
    if payload is None:
        return {
            "planned": planned,
            "summary_present": False,
            "selected": 0,
            "processed": 0,
            "finalized": 0,
            "rejected": 0,
            "errors": 0,
            "warnings": 0,
        }

    selected = safe_int(payload.get("selected"))
    processed = safe_int(payload.get("processed"))
    finalized = safe_int(payload.get("finalized"), derive_result_count(payload, "finalized"))
    rejected = safe_int(payload.get("rejected"), derive_result_count(payload, "rejected"))
    errors = safe_int(payload.get("errors"), derive_result_count(payload, "error"))

    if payload.get("warnings") is None:
        results = payload.get("results")
        warnings = sum(
            1
            for item in results
            if isinstance(item, dict) and item.get("warning")
        ) if isinstance(results, list) else 0
    else:
        warnings = safe_int(payload.get("warnings"))

    return {
        "planned": planned,
        "summary_present": True,
        "selected": selected,
        "processed": processed,
        "finalized": finalized,
        "rejected": rejected,
        "errors": errors,
        "warnings": warnings,
    }


def sanitize_queue(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {"pending": 0, "bytes": 0, "preserved_unsupported": 0}
    return {
        "pending": safe_int(value.get("pending")),
        "bytes": safe_int(value.get("bytes")),
        "preserved_unsupported": safe_int(value.get("preserved_unsupported")),
    }


def sanitize_plan(plan: dict[str, Any] | None) -> dict[str, Any]:
    if plan is None:
        batches = {key: 0 for key in BATCH_KEYS}
        return {
            "available": False,
            "planner_version": None,
            "heavy_choice": "unknown",
            "batches": batches,
            "queues": {},
            "totals": {
                "raw_pending_files": 0,
                "supported_pending_files": 0,
                "preserved_unsupported_files": 0,
                "pending_bytes": 0,
                "planned_files": 0,
            },
        }

    raw_batches = plan.get("batches") if isinstance(plan.get("batches"), dict) else {}
    batches = {key: safe_int(raw_batches.get(source)) for key, source in BATCH_KEYS.items()}
    raw_queues = plan.get("queues") if isinstance(plan.get("queues"), dict) else {}
    queues = {
        key: sanitize_queue(raw_queues.get(key))
        for key in ("links", "images", "audio", "videos", "text", "documents", "spreadsheets")
    }
    raw_totals = plan.get("totals") if isinstance(plan.get("totals"), dict) else {}
    totals = {
        "raw_pending_files": safe_int(raw_totals.get("raw_pending_files")),
        "supported_pending_files": safe_int(raw_totals.get("supported_pending_files")),
        "preserved_unsupported_files": safe_int(raw_totals.get("preserved_unsupported_files")),
        "pending_bytes": safe_int(raw_totals.get("pending_bytes")),
        "planned_files": safe_int(raw_totals.get("planned_files")),
    }
    heavy = plan.get("heavy_choice")
    if heavy not in {"audio", "video", "none"}:
        heavy = "unknown"
    planner_version = plan.get("planner_version")
    if not isinstance(planner_version, str) or len(planner_version) > 32:
        planner_version = None
    return {
        "available": True,
        "planner_version": planner_version,
        "heavy_choice": heavy,
        "batches": batches,
        "queues": queues,
        "totals": totals,
    }


def dependency_groups(batches: dict[str, int]) -> dict[str, bool]:
    media = any(batches.get(key, 0) > 0 for key in ("images", "audio", "videos"))
    visual_ocr = any(batches.get(key, 0) > 0 for key in ("images", "videos"))
    transcription = any(batches.get(key, 0) > 0 for key in ("audio", "videos"))
    return {
        "storage_rclone": True,
        "ffmpeg": media,
        "tesseract": visual_ocr,
        "poppler": batches.get("content", 0) > 0,
        "transcription_python": transcription,
        "spreadsheet_python": batches.get("spreadsheets", 0) > 0,
    }


def build_report(
    *,
    plan_path: Path,
    workspace_root: Path,
    run_id: str,
    run_attempt: int,
    job_status: str,
    provider: str,
    started_epoch: int,
    finished_epoch: int,
) -> dict[str, Any]:
    plan = sanitize_plan(read_json(plan_path))
    batches = plan["batches"]

    pipelines: dict[str, Any] = {}
    for key, (folder, filename) in BRIDGE_SUMMARIES.items():
        pipelines[key] = aggregate_bridge(
            workspace_root / folder / filename,
            batches.get(key, 0),
        )

    totals = {
        "planned": sum(item["planned"] for item in pipelines.values()),
        "selected": sum(item["selected"] for item in pipelines.values()),
        "processed": sum(item["processed"] for item in pipelines.values()),
        "finalized": sum(item["finalized"] for item in pipelines.values()),
        "rejected": sum(item["rejected"] for item in pipelines.values()),
        "errors": sum(item["errors"] for item in pipelines.values()),
        "warnings": sum(item["warnings"] for item in pipelines.values()),
        "summaries_present": sum(1 for item in pipelines.values() if item["summary_present"]),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "report_version": REPORT_VERSION,
        "run": {
            "id": run_id,
            "attempt": run_attempt,
            "status": job_status,
            "provider": provider,
            "started_at": utc_iso(started_epoch),
            "finished_at": utc_iso(finished_epoch),
            "duration_seconds": max(0, finished_epoch - started_epoch),
        },
        "plan": plan,
        "dependencies": dependency_groups(batches),
        "pipelines": pipelines,
        "totals": totals,
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


def upload_report(remote: str, destination: str, local_path: Path, run_id: str, attempt: int) -> bool:
    if not shutil.which("rclone"):
        return False
    base = join_remote(remote, destination)
    if run_rclone(["mkdir", base, "--log-level", "ERROR"]).returncode != 0:
        return False
    remote_path = f"{base}/run-{run_id}-attempt-{attempt}.json"
    return run_rclone([
        "copyto", str(local_path), remote_path,
        "--log-level", "ERROR", "--stats", "0",
    ]).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build privacy-safe aggregate telemetry for one ingest workflow run.")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--job-status", default="unknown")
    parser.add_argument("--provider", default="rclone")
    parser.add_argument("--started-epoch", type=int, required=True)
    parser.add_argument("--finished-epoch", type=int)
    parser.add_argument("--remote")
    parser.add_argument("--destination", default=DEFAULT_DESTINATION)
    args = parser.parse_args()

    if not args.run_id.isdigit() or len(args.run_id) > 32:
        parser.error("--run-id must be a numeric GitHub-style run identifier")
    if not 1 <= args.run_attempt <= 1000:
        parser.error("--run-attempt must be between 1 and 1000")
    if args.started_epoch < 0:
        parser.error("--started-epoch must be non-negative")

    finished_epoch = args.finished_epoch if args.finished_epoch is not None else int(time.time())
    if finished_epoch < 0:
        parser.error("--finished-epoch must be non-negative")

    status = args.job_status if args.job_status in {"success", "failure", "cancelled", "unknown"} else "unknown"
    provider = args.provider if SAFE_PROVIDER_RE.fullmatch(args.provider or "") else "rclone"

    report = build_report(
        plan_path=args.plan.resolve(),
        workspace_root=args.workspace_root.resolve(),
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        job_status=status,
        provider=provider,
        started_epoch=args.started_epoch,
        finished_epoch=finished_epoch,
    )
    if encoded_size(report) > MAX_REPORT_BYTES:
        print("run_report_error code=report_budget_exceeded")
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    uploaded = False
    if args.remote:
        uploaded = upload_report(args.remote, args.destination, args.out, args.run_id, args.run_attempt)
        if not uploaded:
            print("run_report_warning code=upload_failed")
            return 2

    print(
        "run_report_ok "
        f"run_id={args.run_id} attempt={args.run_attempt} duration={report['run']['duration_seconds']} "
        f"planned={report['totals']['planned']} processed={report['totals']['processed']} "
        f"finalized={report['totals']['finalized']} errors={report['totals']['errors']} "
        f"uploaded={1 if uploaded else 0}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
