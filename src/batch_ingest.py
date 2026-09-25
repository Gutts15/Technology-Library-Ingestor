#!/usr/bin/env python3
"""Bounded sequential batch orchestrator for Technology Library ingestion.

The orchestrator deliberately treats each source as an independent checkpoint.
A failed item does not erase successful work, and already-completed items can be
skipped on a later run when the source hash and pipeline/compactor versions match.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evidence_compactor import COMPACTOR_VERSION, build_evidence_summary
from ingest_video import PIPELINE_VERSION, sha256_file

SCHEMA_VERSION = 1
DEFAULT_BATCH_SIZE = 5
MAX_BATCH_SIZE = 20


def package_id(source_id: str | None, source_sha256: str) -> str:
    identity = source_id if source_id else source_sha256
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def checkpoint_matches(checkpoint: dict[str, Any] | None, source_sha256: str, package_dir: Path) -> bool:
    if not checkpoint:
        return False
    evidence = read_json(package_dir / "evidence-summary.json")
    return (
        checkpoint.get("status") == "processed"
        and checkpoint.get("source_sha256") == source_sha256
        and checkpoint.get("pipeline_version") == PIPELINE_VERSION
        and checkpoint.get("compactor_version") == COMPACTOR_VERSION
        and (package_dir / "ingest.json").is_file()
        and evidence is not None
        and evidence.get("compactor_version") == COMPACTOR_VERSION
    )


def build_ingestor_command(
    source: Path,
    package_dir: Path,
    source_id: str | None,
    args: argparse.Namespace,
) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).with_name("ingest_video.py")),
        str(source),
        "--out",
        str(package_dir),
        "--max-keyframes",
        str(args.max_keyframes),
        "--scene-threshold",
        str(args.scene_threshold),
        "--dedupe-distance",
        str(args.dedupe_distance),
    ]
    if source_id:
        command.extend(["--source-id", source_id])
    if args.ocr:
        command.append("--ocr")
        command.extend(["--ocr-languages", args.ocr_languages])
    if args.transcribe:
        command.append("--transcribe")
        command.extend(["--whisper-model", args.whisper_model])
        if args.language:
            command.extend(["--language", args.language])
    return command


def write_error_checkpoint(
    checkpoint_path: Path,
    source_sha256: str,
    now: str,
    error_code: str,
) -> None:
    write_json(
        checkpoint_path,
        {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "source_sha256": source_sha256,
            "pipeline_version": PIPELINE_VERSION,
            "compactor_version": COMPACTOR_VERSION,
            "updated_at": now,
            "error_code": error_code,
        },
    )


def process_item(
    item: dict[str, Any],
    output_root: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    source_value = item.get("source")
    source_id = item.get("source_id")
    if not isinstance(source_value, str) or not source_value:
        return {"package_id": None, "status": "error", "error_code": "invalid_source"}
    if source_id is not None and not isinstance(source_id, str):
        return {"package_id": None, "status": "error", "error_code": "invalid_source_id"}

    source = Path(source_value).resolve()
    if not source.is_file():
        identity = source_id or "missing-source"
        safe_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        return {"package_id": safe_id, "status": "error", "error_code": "source_missing"}

    source_sha256 = sha256_file(source)
    safe_id = package_id(source_id, source_sha256)
    package_dir = output_root / safe_id
    checkpoint_path = package_dir / "checkpoint.json"
    checkpoint = read_json(checkpoint_path)

    if checkpoint_matches(checkpoint, source_sha256, package_dir):
        return {"package_id": safe_id, "status": "skipped", "reason": "checkpoint_match"}

    package_dir.mkdir(parents=True, exist_ok=True)
    command = build_ingestor_command(source, package_dir, source_id, args)
    result = subprocess.run(command, check=False, capture_output=True, text=True)

    now = datetime.now(timezone.utc).isoformat()
    if result.returncode != 0:
        write_error_checkpoint(checkpoint_path, source_sha256, now, "ingestor_exit_nonzero")
        return {"package_id": safe_id, "status": "error", "error_code": "ingestor_exit_nonzero"}

    ingest = read_json(package_dir / "ingest.json")
    if not ingest or ingest.get("processing", {}).get("status") != "processed":
        write_error_checkpoint(checkpoint_path, source_sha256, now, "invalid_ingest_manifest")
        return {"package_id": safe_id, "status": "error", "error_code": "invalid_ingest_manifest"}

    try:
        evidence = build_evidence_summary(package_dir)
    except Exception:
        write_error_checkpoint(checkpoint_path, source_sha256, now, "evidence_compaction_failed")
        return {"package_id": safe_id, "status": "error", "error_code": "evidence_compaction_failed"}

    if evidence.get("compactor_version") != COMPACTOR_VERSION:
        write_error_checkpoint(checkpoint_path, source_sha256, now, "invalid_evidence_summary")
        return {"package_id": safe_id, "status": "error", "error_code": "invalid_evidence_summary"}

    write_json(
        checkpoint_path,
        {
            "schema_version": SCHEMA_VERSION,
            "status": "processed",
            "source_sha256": source_sha256,
            "pipeline_version": PIPELINE_VERSION,
            "compactor_version": COMPACTOR_VERSION,
            "updated_at": now,
        },
    )
    return {"package_id": safe_id, "status": "processed"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Process a bounded Technology Library ingestion queue.")
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-keyframes", type=int, default=12)
    parser.add_argument("--scene-threshold", type=float, default=0.30)
    parser.add_argument("--dedupe-distance", type=int, default=6)
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--ocr-languages", default="eng+por")
    parser.add_argument("--transcribe", action="store_true")
    parser.add_argument("--whisper-model", default="base")
    parser.add_argument("--language", default=None)
    args = parser.parse_args()

    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        parser.error(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")

    queue = read_json(args.queue)
    if not queue or not isinstance(queue.get("items"), list):
        parser.error("queue must contain an items array")

    args.out.mkdir(parents=True, exist_ok=True)
    selected_items = queue["items"][: args.batch_size]
    results = [process_item(item, args.out, args) for item in selected_items]

    counts = {
        "processed": sum(result["status"] == "processed" for result in results),
        "skipped": sum(result["status"] == "skipped" for result in results),
        "errors": sum(result["status"] == "error" for result in results),
    }
    summary = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "compactor_version": COMPACTOR_VERSION,
        "batch_size": len(selected_items),
        "counts": counts,
        "results": results,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(args.out / "batch-summary.json", summary)

    print(
        "batch_ok "
        f"items={len(selected_items)} "
        f"processed={counts['processed']} "
        f"skipped={counts['skipped']} "
        f"errors={counts['errors']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
