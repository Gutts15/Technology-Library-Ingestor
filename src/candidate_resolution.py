#!/usr/bin/env python3
"""Route semantically reviewed candidates without publishing canonical knowledge.

Accepted semantic decisions are bound to the current candidate SHA through
candidate_semantic_contract.py. This stage moves private candidate files into a
lifecycle bucket and writes one sanitized private state document. It never writes
to 00_LIBRARY and never physically deletes suspected accidental content.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_semantic_contract import validate_semantic_payload

SCHEMA_VERSION = 1
RESOLUTION_VERSION = "0.2.0"
DEFAULT_ROOT = "99_INBOX/CANDIDATES"
DEFAULT_STATE = "resolution-state.json"

READY_DECISIONS = {"VALIDATED_NEW", "VALIDATED_UPDATE", "SOURCE_ONLY"}
RESOLVED_DECISIONS = {"DUPLICATE", "REJECTED"}
REVIEW_DECISIONS = {"SPLIT_REQUIRED", "SUSPECTED_ACCIDENTAL", "NEEDS_REVIEW"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["rclone", *arguments], check=False, capture_output=True
    )


def candidate_map(index: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    if not isinstance(index, dict) or index.get("schema_version") != 1:
        raise ValueError("candidate_index_invalid")
    entries = index.get("entries")
    if not isinstance(entries, list):
        raise ValueError("candidate_index_invalid")
    output: dict[str, dict[str, str]] = {}
    for raw in entries:
        if not isinstance(raw, dict) or raw.get("valid") is not True:
            continue
        candidate_id = raw.get("candidate_id")
        sha256 = raw.get("sha256")
        path = raw.get("path")
        if not all(isinstance(value, str) and value for value in (candidate_id, sha256, path)):
            continue
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or len(pure.parts) < 2:
            continue
        if pure.parts[0] != "CHAT_RESEARCH" or not pure.name.startswith("candidate-") or pure.suffix != ".md":
            continue
        output[candidate_id] = {"sha256": sha256, "path": str(pure), "name": pure.name}
    return output


def destination_for(decision: str, name: str) -> str:
    if decision in READY_DECISIONS:
        return str(PurePosixPath("READY_FOR_CURATION") / name)
    if decision in RESOLVED_DECISIONS:
        return str(PurePosixPath("RESOLVED") / decision / name)
    if decision in REVIEW_DECISIONS:
        return str(PurePosixPath("NEEDS_REVIEW") / decision / name)
    raise ValueError("unsupported_decision")


def build_plan(index: dict[str, Any] | None, semantic: dict[str, Any] | None) -> tuple[dict[str, Any] | None, list[str]]:
    accepted, errors = validate_semantic_payload(semantic, index)
    if errors:
        return None, errors
    try:
        current = candidate_map(index)
    except ValueError as exc:
        return None, [str(exc)]

    items: list[dict[str, str]] = []
    for decision in accepted:
        candidate_id = decision["candidate_id"]
        info = current.get(candidate_id)
        if info is None:
            return None, ["resolution_candidate_path_missing"]
        items.append(
            {
                "candidate_id": candidate_id,
                "candidate_sha256": decision["candidate_sha256"],
                "decision": decision["decision"],
                "evidence_level": decision["evidence_level"],
                "source_path": info["path"],
                "destination_path": destination_for(decision["decision"], info["name"]),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "resolution_version": RESOLUTION_VERSION,
        "generated_at": utc_now(),
        "items": items,
        "canonical_write_performed": False,
        "physical_delete_performed": False,
    }, []


def remote_bytes(remote: str, relative: str) -> bytes | None:
    result = run_rclone(["cat", join_remote(remote, relative), "--log-level", "ERROR"])
    return result.stdout if result.returncode == 0 else None


def ensure_dir(remote: str, relative: str) -> bool:
    result = run_rclone(["mkdir", join_remote(remote, relative), "--log-level", "ERROR"])
    return result.returncode == 0


def apply_item(remote: str, root: str, item: dict[str, str]) -> str | None:
    source = str(PurePosixPath(root) / item["source_path"])
    destination = str(PurePosixPath(root) / item["destination_path"])
    expected_sha = item["candidate_sha256"]

    source_bytes = remote_bytes(remote, source)
    destination_bytes = remote_bytes(remote, destination)

    if source_bytes is None:
        if destination_bytes is not None and hashlib.sha256(destination_bytes).hexdigest() == expected_sha:
            return None
        return "source_missing"
    if hashlib.sha256(source_bytes).hexdigest() != expected_sha:
        return "source_revision_changed"
    if destination_bytes is not None:
        if hashlib.sha256(destination_bytes).hexdigest() != expected_sha:
            return "destination_conflict"
        # Destination already has the exact bytes. Remove only the staging copy;
        # this is lifecycle movement, never deletion of original uploaded evidence.
        purge = run_rclone(["deletefile", join_remote(remote, source), "--log-level", "ERROR"])
        return None if purge.returncode == 0 else "source_cleanup_failed"

    parent = str(PurePosixPath(destination).parent)
    if not ensure_dir(remote, parent):
        return "destination_directory_failed"
    move = run_rclone([
        "moveto",
        join_remote(remote, source),
        join_remote(remote, destination),
        "--log-level",
        "ERROR",
    ])
    if move.returncode != 0:
        return "move_failed"
    written = remote_bytes(remote, destination)
    if written is None or hashlib.sha256(written).hexdigest() != expected_sha:
        return "destination_verify_failed"
    return None


def persist_state(remote: str, root: str, name: str, plan: dict[str, Any]) -> bool:
    with tempfile.TemporaryDirectory(prefix="tl-candidate-resolution-") as temp_dir:
        path = Path(temp_dir) / name
        sanitized = {
            "schema_version": plan["schema_version"],
            "resolution_version": plan["resolution_version"],
            "generated_at": plan["generated_at"],
            "items": [
                {
                    "candidate_id": item["candidate_id"],
                    "candidate_sha256": item["candidate_sha256"],
                    "decision": item["decision"],
                    "evidence_level": item["evidence_level"],
                    "destination_bucket": item["destination_path"].split("/", 1)[0],
                }
                for item in plan["items"]
            ],
            "canonical_write_performed": False,
            "physical_delete_performed": False,
        }
        path.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result = run_rclone([
            "copyto",
            str(path),
            join_remote(remote, str(PurePosixPath(root) / name)),
            "--log-level",
            "ERROR",
            "--stats",
            "0",
        ])
        return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan/apply candidate lifecycle routing after semantic review.")
    parser.add_argument("--candidate-index", type=Path, required=True)
    parser.add_argument("--semantic-decisions", type=Path, required=True)
    parser.add_argument("--remote", default="tl:")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--state-name", default=DEFAULT_STATE)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    index = read_json(args.candidate_index)
    semantic = read_json(args.semantic_decisions)
    plan, errors = build_plan(index, semantic)
    if plan is None:
        print(f"candidate_resolution_error codes={','.join(sorted(set(errors)))} canonical_write=0")
        return 2

    if not args.apply:
        counts: dict[str, int] = {}
        for item in plan["items"]:
            bucket = item["destination_path"].split("/", 1)[0]
            counts[bucket] = counts.get(bucket, 0) + 1
        print(
            "candidate_resolution_ok mode=plan "
            f"items={len(plan['items'])} ready={counts.get('READY_FOR_CURATION', 0)} "
            f"review={counts.get('NEEDS_REVIEW', 0)} resolved={counts.get('RESOLVED', 0)} canonical_write=0"
        )
        return 0

    if not shutil.which("rclone"):
        print("candidate_resolution_error codes=rclone_missing canonical_write=0")
        return 2

    failures: list[str] = []
    for item in plan["items"]:
        error = apply_item(args.remote, args.root, item)
        if error:
            failures.append(error)
    if failures:
        print(f"candidate_resolution_error codes={','.join(sorted(set(failures)))} canonical_write=0")
        return 2
    if not persist_state(args.remote, args.root, args.state_name, plan):
        print("candidate_resolution_error codes=state_write_failed canonical_write=0")
        return 2

    print(
        "candidate_resolution_ok mode=apply "
        f"items={len(plan['items'])} canonical_write=0 physical_delete=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
