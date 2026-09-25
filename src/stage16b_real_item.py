#!/usr/bin/env python3
"""Run one exact real DROP_HERE item through private Stage 16A–16B.

Default selection remains conservative for small link/text inputs. An exact
--only-name may also select one bounded video. Videos require an explicit public
--source-url because binary media does not reliably preserve provenance URLs in
the compact evidence summary.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from candidate_source_probe import extract_urls, normalize_url
from media_router import (
    TARGETS, classify_item, destination_candidates, item_name, item_remote_path,
    join_remote, list_items, move_without_overwrite, remote_exists, run_rclone,
)

DROP = "99_INBOX/DROP_HERE"
MAX_SMALL_INPUT_BYTES = 16 * 1024
MAX_VIDEO_INPUT_BYTES = 64 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]
REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
SMALL_SUFFIXES = {".url", ".webloc", ".txt", ".md"}


def progress(stage: str) -> None:
    print(f"stage16b_real_item_progress stage={stage}", flush=True)


def video_dependency_error() -> str | None:
    if shutil.which("ffmpeg") is None:
        return "ffmpeg_missing"
    if shutil.which("ffprobe") is None:
        return "ffprobe_missing"
    return None


def select_small_input(items: list[dict[str, Any]] | None) -> tuple[dict[str, Any] | None, str | None]:
    if items is None:
        return None, "drop_list_failed"
    eligible = []
    for item in items:
        name = item_name(item)
        kind = classify_item(item)
        size = item.get("Size")
        if (
            name
            and name not in {".", ".."}
            and "/" not in name
            and "\\" not in name
            and kind in {"link", "text"}
            and Path(name).suffix.lower() in SMALL_SUFFIXES
            and isinstance(size, int)
            and 1 <= size <= MAX_SMALL_INPUT_BYTES
        ):
            eligible.append((0 if kind == "link" else 1, size, name, item))
    if not eligible:
        return None, "small_link_or_text_missing"
    eligible.sort(key=lambda entry: entry[:3])
    return eligible[0][3], None


def select_exact_input(
    items: list[dict[str, Any]] | None,
    only_name: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if items is None:
        return None, "drop_list_failed"
    if not only_name or only_name in {".", ".."} or "/" in only_name or "\\" in only_name:
        return None, "selected_item_invalid"
    matches = [item for item in items if item_name(item) == only_name]
    if len(matches) != 1:
        return None, "selected_item_missing"
    item = matches[0]
    kind = classify_item(item)
    size = item.get("Size")
    if kind in {"link", "text"}:
        if Path(only_name).suffix.lower() not in SMALL_SUFFIXES or not isinstance(size, int) or not 1 <= size <= MAX_SMALL_INPUT_BYTES:
            return None, "selected_item_not_bounded"
    elif kind == "video":
        if Path(only_name).suffix.lower() != ".mp4" or not isinstance(size, int) or not 1 <= size <= MAX_VIDEO_INPUT_BYTES:
            return None, "selected_video_not_bounded"
    else:
        return None, "selected_item_unsupported"
    return item, None


def run_step(arguments: list[str], stage: str) -> bool:
    stop = threading.Event()

    def heartbeat() -> None:
        elapsed = 0
        while not stop.wait(30):
            elapsed += 30
            print(f"stage16b_real_item_waiting stage={stage} elapsed_s={elapsed}", flush=True)

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        result = subprocess.run(arguments, cwd=ROOT, capture_output=True, timeout=1800)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        stop.set()
        thread.join()


def decode_small_input(raw: bytes) -> str | None:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def package_from_summary(workspace: Path, kind: str) -> str | None:
    name = "bridge-summary.json" if kind in {"link", "video"} else "content-bridge-summary.json"
    try:
        summary = json.loads((workspace / name).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(summary, dict) or summary.get("errors", 0) != 0 or summary.get("selected") != 1:
        return None
    if kind == "link":
        if summary.get("processed", 0) + summary.get("finalized", 0) != 1:
            return None
        packages = summary.get("packages")
        return packages[0] if isinstance(packages, list) and len(packages) == 1 and isinstance(packages[0], str) else None
    results = summary.get("results")
    if not isinstance(results, list) or len(results) != 1 or results[0].get("status") not in {"processed", "finalized"}:
        return None
    package_id = results[0].get("package_id")
    return package_id if isinstance(package_id, str) else None


def bridge_result(path: Path) -> tuple[str, str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("canonical_write") != 0:
        return None
    outcome = payload.get("outcome")
    reason = payload.get("reason_code")
    if outcome not in {"created", "existing", "held"} or not isinstance(reason, str) or not REASON_RE.fullmatch(reason):
        return None
    return outcome, reason


def run(
    remote: str,
    *,
    only_name: str | None = None,
    source_url: str | None = None,
) -> tuple[str, str]:
    progress("drop_scan")
    items = list_items(remote, DROP)
    if only_name is None:
        item, error = select_small_input(items)
    else:
        item, error = select_exact_input(items, only_name)
    if error or item is None:
        return "held", error or "drop_list_failed"

    name = item_name(item)
    source = item_remote_path(remote, DROP, item)
    if name is None or source is None:
        return "held", "input_identity_invalid"

    kind = classify_item(item)
    if source_url is not None and normalize_url(source_url) is None:
        return "held", "public_source_invalid"
    if kind == "video" and source_url is None:
        return "held", "video_public_source_required"
    if kind == "video":
        dependency_error = video_dependency_error()
        if dependency_error is not None:
            return "held", dependency_error

    progress("input_check")
    if kind in {"link", "text"}:
        raw_result = subprocess.run(["rclone", "cat", source, "--log-level", "ERROR"], capture_output=True)
        if raw_result.returncode != 0 or len(raw_result.stdout) > MAX_SMALL_INPUT_BYTES:
            return "held", "input_read_failed"
        input_text = decode_small_input(raw_result.stdout)
        if input_text is None:
            return "held", "input_read_failed"
        if source_url is None and not extract_urls(input_text, limit=1):
            return "held", "input_public_url_missing"

    target_queue = TARGETS[kind]
    if run_rclone(["mkdir", join_remote(remote, target_queue), "--log-level", "ERROR"]).returncode != 0:
        return "held", "routed_queue_unavailable"
    available = next((path for path in destination_candidates(remote, target_queue, item) if not remote_exists(path)), None)
    if available is None:
        return "held", "routed_queue_conflict"
    if move_without_overwrite(source, [available]) != "moved":
        return "held", "input_route_failed"
    routed_name = available.rsplit("/", 1)[-1]
    progress("input_routed")

    with tempfile.TemporaryDirectory(prefix="tl-stage16b-") as temporary:
        workspace = Path(temporary)
        if kind == "link":
            processor = [
                sys.executable, str(ROOT / "src/link_bridge.py"),
                "--remote", remote, "--workspace", str(workspace),
                "--only-name", routed_name, "--batch-size", "1",
            ]
        elif kind == "text":
            processor = [
                sys.executable, str(ROOT / "src/text_document_bridge.py"),
                "--remote", remote, "--workspace", str(workspace),
                "--only-name", routed_name, "--batch-size", "1",
            ]
        else:
            processor = [
                sys.executable, str(ROOT / "src/rclone_bridge.py"),
                "--remote", remote, "--workspace", str(workspace),
                "--only-name", routed_name, "--batch-size", "1",
                "--max-keyframes", "12", "--ocr", "--ocr-languages", "eng+por",
                "--transcribe", "--whisper-model", "base",
            ]

        progress("processor")
        if not run_step(processor, "processor"):
            return "held", "input_processing_failed"
        package_id = package_from_summary(workspace, kind)
        if package_id is None:
            return "held", "processed_package_missing"

        stages = [
            [sys.executable, str(ROOT / "src/ready_index.py"), "--remote", remote,
             "--out", str(workspace / "ready-index.json"), "--max-packages", "500"],
            [sys.executable, str(ROOT / "src/curation_handoff.py"), "--remote", remote, "--max-handoff", "200"],
            [sys.executable, str(ROOT / "src/ready_evidence_bridge.py"), "--remote", remote, "--max-items", "200"],
        ]
        for number, command in enumerate(stages, 1):
            stage = ("ready_index", "curation_handoff", "file_evidence")[number - 1]
            progress(stage)
            if not run_step(command, stage):
                return "held", f"evidence_stage_{number}_failed"

        private_report = workspace / "candidate-bridge-report.json"
        bridge = [
            sys.executable, str(ROOT / "src/file_evidence_candidate_bridge.py"),
            "--remote", remote, "--package-id", package_id, "--report", str(private_report),
        ]
        if source_url is not None:
            bridge.extend(["--source-url", source_url])

        progress("candidate_bridge")
        bridge_ok = run_step(bridge, "candidate_bridge")
        result = bridge_result(private_report)
        if result is None:
            return "held", "candidate_bridge_report_missing"
        if not bridge_ok or result[0] == "held":
            return "held", result[1]
    return result[0], "stage16b_complete"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one bounded real DROP_HERE item through private Stage 16B.")
    parser.add_argument("--remote", default="tl:")
    parser.add_argument("--only-name", help="Exact DROP_HERE filename. Required to select a video.")
    parser.add_argument("--source-url", help="Explicit public provenance URL for the selected item.")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.report is not None and any(part.upper() == "00_LIBRARY" for part in args.report.parts):
        parser.error("--report cannot target 00_LIBRARY")
    if not shutil.which("rclone"):
        outcome, reason = "held", "rclone_missing"
    else:
        outcome, reason = run(args.remote, only_name=args.only_name, source_url=args.source_url)
    if args.report is not None:
        sanitized = {
            "schema_version": 1, "stage": "16A-16B", "outcome": outcome,
            "reason_code": reason, "canonical_write": 0, "paid_model": 0,
        }
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(sanitized, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            print("stage16b_real_item_hold outcome=held reason=report_write_failed canonical_write=0 paid_model=0")
            return 2
    print(
        f"stage16b_real_item_{'ok' if outcome != 'held' else 'hold'} "
        f"outcome={outcome} reason={reason} canonical_write=0 paid_model=0"
    )
    return 0 if outcome != "held" else 2


if __name__ == "__main__":
    raise SystemExit(main())
