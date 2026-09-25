#!/usr/bin/env python3
"""Privacy-safe workload planning for the multi-pipeline private ingest job.

The planner inspects queue metadata only through rclone. It emits aggregate
counts/bytes and per-pipeline batch limits, never private filenames. V1 keeps
cheap/moderate queues bounded and allows only one transcription-heavy queue
(AUDIO or VIDEO) per workflow run, chosen by the oldest supported pending item.

Only formats that the matching bridge can actually process count toward a batch.
Unsupported-but-routed files remain visible as aggregate preserved counts and do
not wake a processor that has no executable work.

This is a deterministic execution budget, not a runtime prediction. Media
duration is intentionally not guessed from filename or file size.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audio_bridge import MIME_SUFFIXES as AUDIO_MIME_SUFFIXES
from audio_bridge import SUPPORTED_SUFFIXES as AUDIO_SUFFIXES
from image_bridge import MIME_SUFFIXES as IMAGE_MIME_SUFFIXES
from image_bridge import SUPPORTED_SUFFIXES as IMAGE_SUFFIXES
from link_bridge import MIME_SUFFIXES as LINK_MIME_SUFFIXES
from link_bridge import SUPPORTED_SUFFIXES as LINK_SUFFIXES
from rclone_bridge import SUPPORTED_SUFFIXES as VIDEO_SUFFIXES
from rclone_bridge import VIDEO_MIME_SUFFIXES
from spreadsheet_bridge import MIME_SUFFIXES as SPREADSHEET_MIME_SUFFIXES
from spreadsheet_bridge import SUPPORTED_SUFFIXES as SPREADSHEET_SUFFIXES
from text_document_bridge import DOCUMENT_MIME_SUFFIXES, TEXT_MIME_TYPES
from text_document_bridge import DOCUMENT_SUFFIXES, TEXT_SUFFIXES

PLANNER_VERSION = "0.1.1"
SCHEMA_VERSION = 1

QUEUES = {
    "links": "99_INBOX/TO_REVIEW/LINKS",
    "images": "99_INBOX/TO_REVIEW/IMAGES",
    "audio": "99_INBOX/TO_REVIEW/AUDIO",
    "videos": "99_INBOX/TO_REVIEW/VIDEOS",
    "text": "99_INBOX/TO_REVIEW/TEXT",
    "documents": "99_INBOX/TO_REVIEW/DOCUMENTS",
    "spreadsheets": "99_INBOX/TO_REVIEW/SPREADSHEETS",
}

MAX_BATCH = {
    "links": 20,
    "images": 10,
    "content": 10,
    "spreadsheets": 5,
    "audio": 1,
    "videos": 3,
}


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def ensure_remote_dir(remote_root: str, relative: str) -> bool:
    return run_rclone(["mkdir", join_remote(remote_root, relative), "--log-level", "ERROR"]).returncode == 0


def list_queue(remote_root: str, relative: str) -> list[dict[str, Any]] | None:
    result = run_rclone([
        "lsjson", join_remote(remote_root, relative), "--files-only", "--log-level", "ERROR",
    ])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    return [item for item in payload if isinstance(item, dict)]


def safe_size(item: dict[str, Any]) -> int:
    try:
        return max(0, int(item.get("Size") or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def item_suffix(item: dict[str, Any]) -> str:
    raw = item.get("Name") or item.get("Path")
    if not isinstance(raw, str):
        return ""
    return Path(raw).suffix.lower()


def item_mime(item: dict[str, Any]) -> str:
    value = item.get("MimeType")
    return value.lower().strip() if isinstance(value, str) else ""


def is_supported(item: dict[str, Any], kind: str) -> bool:
    suffix = item_suffix(item)
    mime = item_mime(item)

    if kind == "links":
        return suffix in LINK_SUFFIXES or mime in LINK_MIME_SUFFIXES
    if kind == "images":
        return suffix in IMAGE_SUFFIXES or mime in IMAGE_MIME_SUFFIXES
    if kind == "audio":
        return suffix in AUDIO_SUFFIXES or mime in AUDIO_MIME_SUFFIXES
    if kind == "videos":
        return suffix in VIDEO_SUFFIXES or mime in VIDEO_MIME_SUFFIXES
    if kind == "text":
        return suffix in TEXT_SUFFIXES or mime.startswith("text/") or mime in TEXT_MIME_TYPES
    if kind == "documents":
        return suffix in DOCUMENT_SUFFIXES or mime in DOCUMENT_MIME_SUFFIXES
    if kind == "spreadsheets":
        return suffix in SPREADSHEET_SUFFIXES or mime in SPREADSHEET_MIME_SUFFIXES
    return False


def parse_modtime(value: Any) -> float:
    if not isinstance(value, str) or not value.strip():
        return float("inf")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def supported_items(items: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [item for item in items if is_supported(item, kind)]


def oldest_supported(items: list[dict[str, Any]], kind: str) -> float | None:
    supported = supported_items(items, kind)
    if not supported:
        return None
    return min(parse_modtime(item.get("ModTime")) for item in supported)


def supported_aggregate(items: list[dict[str, Any]], kind: str) -> dict[str, int]:
    supported = supported_items(items, kind)
    return {
        "pending": len(supported),
        "bytes": sum(safe_size(item) for item in supported),
        "preserved_unsupported": len(items) - len(supported),
    }


def choose_heavy(audio_items: list[dict[str, Any]], video_items: list[dict[str, Any]]) -> str:
    audio_oldest = oldest_supported(audio_items, "audio")
    video_oldest = oldest_supported(video_items, "videos")
    if audio_oldest is None and video_oldest is None:
        return "none"
    if audio_oldest is None:
        return "video"
    if video_oldest is None:
        return "audio"
    return "audio" if audio_oldest <= video_oldest else "video"


def build_plan(queues: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    links = supported_aggregate(queues["links"], "links")
    images = supported_aggregate(queues["images"], "images")
    audio = supported_aggregate(queues["audio"], "audio")
    videos = supported_aggregate(queues["videos"], "videos")
    text = supported_aggregate(queues["text"], "text")
    documents = supported_aggregate(queues["documents"], "documents")
    spreadsheets = supported_aggregate(queues["spreadsheets"], "spreadsheets")

    heavy = choose_heavy(queues["audio"], queues["videos"])
    content_pending = text["pending"] + documents["pending"]

    batches = {
        "links": min(links["pending"], MAX_BATCH["links"]),
        "images": min(images["pending"], MAX_BATCH["images"]),
        "content": min(content_pending, MAX_BATCH["content"]),
        "spreadsheets": min(spreadsheets["pending"], MAX_BATCH["spreadsheets"]),
        "audio": min(audio["pending"], MAX_BATCH["audio"]) if heavy == "audio" else 0,
        "videos": min(videos["pending"], MAX_BATCH["videos"]) if heavy == "video" else 0,
    }

    raw_pending = sum(len(items) for items in queues.values())
    supported_pending = sum(
        aggregate["pending"]
        for aggregate in (links, images, audio, videos, text, documents, spreadsheets)
    )
    preserved_unsupported = sum(
        aggregate["preserved_unsupported"]
        for aggregate in (links, images, audio, videos, text, documents, spreadsheets)
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "planner_version": PLANNER_VERSION,
        "policy": {
            "heavy_mode": "oldest_supported_queue",
            "one_heavy_queue_per_run": True,
            "runtime_prediction": False,
            "unsupported_items_wake_processors": False,
        },
        "heavy_choice": heavy,
        "queues": {
            "links": links,
            "images": images,
            "audio": audio,
            "videos": videos,
            "text": text,
            "documents": documents,
            "spreadsheets": spreadsheets,
        },
        "batches": batches,
        "totals": {
            "raw_pending_files": raw_pending,
            "supported_pending_files": supported_pending,
            "preserved_unsupported_files": preserved_unsupported,
            "pending_bytes": sum(safe_size(item) for items in queues.values() for item in items),
            "planned_files": sum(batches.values()),
        },
    }


def env_lines(plan: dict[str, Any]) -> str:
    batches = plan["batches"]
    values = {
        "TL_PLAN_LINK_BATCH": batches["links"],
        "TL_PLAN_IMAGE_BATCH": batches["images"],
        "TL_PLAN_AUDIO_BATCH": batches["audio"],
        "TL_PLAN_VIDEO_BATCH": batches["videos"],
        "TL_PLAN_CONTENT_BATCH": batches["content"],
        "TL_PLAN_SPREADSHEET_BATCH": batches["spreadsheets"],
        "TL_PLAN_HEAVY_KIND": plan["heavy_choice"],
    }
    return "".join(f"{key}={value}\n" for key, value in values.items())


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a privacy-safe bounded execution plan for private ingest queues.")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--env-out", type=Path, required=True)
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("workload_plan_error code=rclone_missing")
        return 2

    queues: dict[str, list[dict[str, Any]]] = {}
    for kind, folder in QUEUES.items():
        if not ensure_remote_dir(args.remote, folder):
            print("workload_plan_error code=remote_directory_unavailable")
            return 2
        items = list_queue(args.remote, folder)
        if items is None:
            print("workload_plan_error code=list_failed")
            return 2
        queues[kind] = items

    plan = build_plan(queues)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.env_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.env_out.write_text(env_lines(plan), encoding="utf-8")

    batches = plan["batches"]
    print(
        "workload_plan_ok "
        f"pending={plan['totals']['supported_pending_files']} "
        f"preserved={plan['totals']['preserved_unsupported_files']} "
        f"planned={plan['totals']['planned_files']} heavy={plan['heavy_choice']} "
        f"links={batches['links']} images={batches['images']} audio={batches['audio']} "
        f"videos={batches['videos']} content={batches['content']} spreadsheets={batches['spreadsheets']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
