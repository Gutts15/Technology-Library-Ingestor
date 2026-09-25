#!/usr/bin/env python3
"""Rebuild missing compact evidence for legacy video READY packages.

This migration never downloads or reprocesses source video. It reconstructs
`evidence-summary.json` only from compact artifacts already stored inside the
READY package: ingest metadata, timeline text, OCR JSON and transcript JSON when
available. Dry-run is the default; `--apply` is required for writes.

Stdout contains aggregate counts only and never source names, package IDs,
transcripts, OCR text or provider IDs.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from evidence_compactor import COMPACTOR_VERSION, MAX_SUMMARY_BYTES, build_evidence_summary
from legacy_ready_backfill import (
    PACKAGE_ID_RE,
    infer_kind,
    join_remote,
    list_remote_packages,
    read_json_file,
    remote_entries,
)

REBUILD_VERSION = "0.1.0"
DEFAULT_SOURCE = "99_INBOX/READY_FOR_ANALYSIS"
COMPACT_INPUTS = (
    "ingest.json",
    "timeline.md",
    "ocr.json",
    "transcript.json",
    "transcript.md",
)


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *arguments], check=False, capture_output=True, text=True
    )


def eligible_files(filenames: set[str]) -> bool:
    return (
        "ingest.json" in filenames
        and "checkpoint.json" in filenames
        and "evidence-summary.json" not in filenames
    )


def local_candidates(root: Path, max_packages: int) -> list[Path]:
    if not root.is_dir():
        return []
    packages = [p for p in root.iterdir() if p.is_dir() and PACKAGE_ID_RE.fullmatch(p.name)]
    packages.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    output: list[Path] = []
    for package in packages[:max_packages]:
        filenames = {p.name for p in package.iterdir() if p.is_file()}
        ingest = read_json_file(package / "ingest.json")
        if eligible_files(filenames) and infer_kind(filenames, ingest) == "video":
            output.append(package)
    return output


def validate_summary(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > MAX_SUMMARY_BYTES:
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("schema_version") == 1
        and payload.get("compactor_version") == COMPACTOR_VERSION
    )


def rebuild_local(package: Path) -> bool:
    try:
        build_evidence_summary(package)
    except Exception:
        return False
    return validate_summary(package / "evidence-summary.json")


def remote_candidates(remote: str, source: str, max_packages: int) -> list[tuple[str, set[str]]] | None:
    package_ids = list_remote_packages(remote, source, max_packages)
    if package_ids is None:
        return None
    output: list[tuple[str, set[str]]] = []
    for package_id in package_ids:
        package_remote = join_remote(remote, f"{source.strip('/')}/{package_id}")
        entries = remote_entries(package_remote)
        if entries is None:
            return None
        filenames = {
            str(item.get("Name"))
            for item in entries
            if not item.get("IsDir") and isinstance(item.get("Name"), str)
        }
        if not eligible_files(filenames):
            continue
        # All current real legacy candidates are video packages. Confirm kind
        # after downloading ingest metadata during apply; dry-run relies on
        # generic video artifact markers only and otherwise counts conservatively.
        if "timeline.md" in filenames or "contact-sheet.jpg" in filenames:
            output.append((package_id, filenames))
        else:
            # Ingest itself is compact and safe to inspect privately.
            with tempfile.TemporaryDirectory(prefix="tl-evidence-kind-") as temp_dir:
                local_ingest = Path(temp_dir) / "ingest.json"
                result = run_rclone([
                    "copyto", f"{package_remote}/ingest.json", str(local_ingest),
                    "--log-level", "ERROR", "--stats", "0",
                ])
                if result.returncode != 0:
                    return None
                ingest = read_json_file(local_ingest)
                if infer_kind(filenames, ingest) == "video":
                    output.append((package_id, filenames))
    return output


def rebuild_remote_package(remote: str, source: str, package_id: str, filenames: set[str]) -> bool:
    package_remote = join_remote(remote, f"{source.strip('/')}/{package_id}")
    with tempfile.TemporaryDirectory(prefix="tl-evidence-rebuild-") as temp_dir:
        local_package = Path(temp_dir) / "package"
        local_package.mkdir(parents=True, exist_ok=True)

        for filename in COMPACT_INPUTS:
            if filename not in filenames:
                continue
            result = run_rclone([
                "copyto", f"{package_remote}/{filename}", str(local_package / filename),
                "--log-level", "ERROR", "--stats", "0",
            ])
            if result.returncode != 0:
                return False

        ingest = read_json_file(local_package / "ingest.json")
        if infer_kind(filenames, ingest) != "video":
            return False

        if not rebuild_local(local_package):
            return False

        result = run_rclone([
            "copyto", str(local_package / "evidence-summary.json"),
            f"{package_remote}/evidence-summary.json",
            "--log-level", "ERROR", "--stats", "0",
        ])
        return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild missing legacy compact evidence without raw media.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root-dir", type=Path)
    mode.add_argument("--remote")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--max-packages", type=int, default=100)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.max_packages <= 500:
        parser.error("--max-packages must be between 1 and 500")

    candidates = 0
    rebuilt = 0
    failed = 0

    if args.root_dir is not None:
        packages = local_candidates(args.root_dir.resolve(), args.max_packages)
        candidates = len(packages)
        if args.apply:
            for package in packages:
                if rebuild_local(package):
                    rebuilt += 1
                else:
                    failed += 1
    else:
        if not shutil.which("rclone"):
            print("legacy_evidence_rebuild_error code=rclone_missing")
            return 2
        packages = remote_candidates(args.remote, args.source, args.max_packages)
        if packages is None:
            print("legacy_evidence_rebuild_error code=remote_audit_failed")
            return 2
        candidates = len(packages)
        if args.apply:
            for package_id, filenames in packages:
                if rebuild_remote_package(args.remote, args.source, package_id, filenames):
                    rebuilt += 1
                else:
                    failed += 1

    mode_name = "apply" if args.apply else "dry_run"
    print(
        "legacy_evidence_rebuild_ok "
        f"version={REBUILD_VERSION} mode={mode_name} candidates={candidates} rebuilt={rebuilt} failed={failed} "
        "raw_media_used=0"
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
