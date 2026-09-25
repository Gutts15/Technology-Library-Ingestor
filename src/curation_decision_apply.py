#!/usr/bin/env python3
"""Apply privacy-safe semantic-curation decisions to the curation handoff.

The durable decision file contains only package IDs, revision keys and terminal
curation states. Evidence text, filenames, URLs, OCR and transcripts are never
accepted here.

A decision is applied only when the package is still pending in the current
handoff and the decision revision matches that pending revision. Old decisions
therefore become harmless stale records if evidence is rebuilt later.

When --require-publish-receipt is enabled, an active `analyzed` decision is applied
only after the private canonical publisher has produced a receipt for the same
package_id + revision_key. Rejected/do-not-reprocess transitions do not require a
canonical publish receipt.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DECISIONS_VERSION = "0.1.0"
PUBLISH_STATE_VERSION = "0.1.0"
MAX_DECISIONS = 200
PACKAGE_ID_RE = re.compile(r"^[0-9a-f]{20}$")
REVISION_RE = re.compile(r"^[0-9a-f]{20}$")
RECORD_ID_RE = re.compile(r"^[0-9a-f]{20}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_STATES = {"analyzed", "rejected", "do_not_reprocess"}
DEFAULT_HANDOFF = "99_INBOX/CURATION/HANDOFF/latest.json"
DEFAULT_PUBLISH_RECEIPT = "99_INBOX/CURATION/PUBLISH_STATE/latest.json"


def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *arguments], check=False, capture_output=True, text=True
    )


def remote_json(remote_path: str) -> dict[str, Any] | None:
    result = run_rclone(["cat", remote_path, "--log-level", "ERROR"])
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def parse_decisions(payload: dict[str, Any] | None) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        raise ValueError("decisions_missing")
    if set(payload) != {"schema_version", "decisions_version", "items"}:
        raise ValueError("decisions_top_level_fields")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("decisions_schema")
    if payload.get("decisions_version") != DECISIONS_VERSION:
        raise ValueError("decisions_version")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > MAX_DECISIONS:
        raise ValueError("decisions_items")

    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict) or set(raw) != {"package_id", "revision_key", "state"}:
            raise ValueError("decision_fields")
        package_id = raw.get("package_id")
        revision_key = raw.get("revision_key")
        state = raw.get("state")
        if not isinstance(package_id, str) or not PACKAGE_ID_RE.fullmatch(package_id):
            raise ValueError("decision_package_id")
        if not isinstance(revision_key, str) or not REVISION_RE.fullmatch(revision_key):
            raise ValueError("decision_revision_key")
        if state not in ALLOWED_STATES:
            raise ValueError("decision_state")
        if package_id in seen:
            raise ValueError("decision_duplicate_package")
        seen.add(package_id)
        output.append(
            {"package_id": package_id, "revision_key": revision_key, "state": str(state)}
        )
    return output


def pending_revisions(payload: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("handoff_invalid")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("handoff_invalid")
    output: dict[str, str] = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        package_id = raw.get("package_id")
        revision_key = raw.get("revision_key")
        if (
            isinstance(package_id, str)
            and PACKAGE_ID_RE.fullmatch(package_id)
            and isinstance(revision_key, str)
            and REVISION_RE.fullmatch(revision_key)
        ):
            output[package_id] = revision_key
    return output


def parse_publish_receipt(payload: dict[str, Any] | None) -> set[tuple[str, str]]:
    if not isinstance(payload, dict):
        raise ValueError("publish_receipt_missing")
    if set(payload) != {
        "schema_version",
        "publish_state_version",
        "publish_version",
        "items",
    }:
        raise ValueError("publish_receipt_fields")
    if payload.get("schema_version") != 1:
        raise ValueError("publish_receipt_schema")
    if payload.get("publish_state_version") != PUBLISH_STATE_VERSION:
        raise ValueError("publish_receipt_version")
    publish_version = payload.get("publish_version")
    if not isinstance(publish_version, str) or not publish_version:
        raise ValueError("publish_receipt_publish_version")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("publish_receipt_items")

    covered: set[tuple[str, str]] = set()
    for raw in items:
        if not isinstance(raw, dict) or set(raw) != {
            "package_id",
            "revision_key",
            "record_id",
            "target_path",
            "content_sha256",
        }:
            raise ValueError("publish_receipt_item_fields")
        package_id = raw.get("package_id")
        revision_key = raw.get("revision_key")
        record_id = raw.get("record_id")
        target_path = raw.get("target_path")
        content_sha = raw.get("content_sha256")
        if not isinstance(package_id, str) or not PACKAGE_ID_RE.fullmatch(package_id):
            raise ValueError("publish_receipt_package_id")
        if not isinstance(revision_key, str) or not REVISION_RE.fullmatch(revision_key):
            raise ValueError("publish_receipt_revision_key")
        if not isinstance(record_id, str) or not RECORD_ID_RE.fullmatch(record_id):
            raise ValueError("publish_receipt_record_id")
        if not isinstance(target_path, str) or not target_path.startswith("00_LIBRARY/"):
            raise ValueError("publish_receipt_target")
        if not isinstance(content_sha, str) or not SHA_RE.fullmatch(content_sha):
            raise ValueError("publish_receipt_sha256")
        covered.add((package_id, revision_key))
    return covered


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply revision-safe privacy-safe curation decisions."
    )
    parser.add_argument("--decisions", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root-dir", type=Path)
    mode.add_argument("--remote")
    parser.add_argument("--handoff", default=DEFAULT_HANDOFF)
    parser.add_argument("--publish-receipt", default=DEFAULT_PUBLISH_RECEIPT)
    parser.add_argument("--require-publish-receipt", action="store_true")
    args = parser.parse_args()

    try:
        decisions = parse_decisions(read_json_file(args.decisions))
    except ValueError as exc:
        print(f"curation_decision_apply_error code={exc}")
        return 2

    if args.root_dir is not None:
        root = args.root_dir.resolve()
        handoff = read_json_file(root / args.handoff)
    else:
        root = None
        handoff = remote_json(join_remote(str(args.remote), args.handoff))

    try:
        pending = pending_revisions(handoff)
    except ValueError as exc:
        print(f"curation_decision_apply_error code={exc}")
        return 2

    active: list[dict[str, str]] = []
    stale = 0
    closed = 0
    for decision in decisions:
        current_revision = pending.get(decision["package_id"])
        if current_revision is None:
            closed += 1
            continue
        if current_revision != decision["revision_key"]:
            stale += 1
            continue
        active.append(decision)

    if not active:
        print(
            "curation_decision_apply_ok "
            f"decisions={len(decisions)} applied=0 stale={stale} closed={closed}"
        )
        return 0

    if args.require_publish_receipt:
        analyzed_active = [item for item in active if item["state"] == "analyzed"]
        if analyzed_active:
            if root is not None:
                receipt_payload = read_json_file(root / args.publish_receipt)
            else:
                receipt_payload = remote_json(
                    join_remote(str(args.remote), args.publish_receipt)
                )
            try:
                covered = parse_publish_receipt(receipt_payload)
            except ValueError as exc:
                print(f"curation_decision_apply_error code={exc}")
                return 2
            for decision in analyzed_active:
                key = (decision["package_id"], decision["revision_key"])
                if key not in covered:
                    print(
                        "curation_decision_apply_error code=publish_receipt_required "
                        f"package_id={decision['package_id']}"
                    )
                    return 2

    handoff_script = Path(__file__).with_name("curation_handoff.py")
    command = [sys.executable, str(handoff_script)]
    if args.root_dir is not None:
        command.extend(["--root-dir", str(args.root_dir)])
    else:
        command.extend(["--remote", str(args.remote)])
    for decision in active:
        command.extend(
            ["--transition", f"{decision['package_id']}={decision['state']}"]
        )

    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        print("curation_decision_apply_error code=handoff_transition_failed")
        if result.stdout.strip():
            print(result.stdout.strip())
        return 2

    if result.stdout.strip():
        print(result.stdout.strip())
    print(
        "curation_decision_apply_ok "
        f"decisions={len(decisions)} applied={len(active)} stale={stale} closed={closed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
