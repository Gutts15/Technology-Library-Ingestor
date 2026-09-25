#!/usr/bin/env python3
"""Maintain privacy-safe curation state and a bounded READY handoff queue.

The curation layer intentionally stores no evidence text, filenames, source
names, URLs, OCR, transcript content or provider IDs. It works only with the
sanitized READY index and records package IDs, generic artifact pointers,
revision metadata and explicit curation states.

States:
- pending: eligible for curation and present in the handoff queue
- analyzed: curation completed for the current evidence revision
- rejected: intentionally not promoted for the current evidence revision
- do_not_reprocess: terminal hold; never automatically reopened

If an analyzed/rejected package later arrives with a different privacy-safe
revision key, it is reopened as pending. `do_not_reprocess` remains terminal.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_VERSION = "0.1.0"
SCHEMA_VERSION = 1
DEFAULT_READY_INDEX = "99_INBOX/CURATION/READY_INDEX/latest.json"
DEFAULT_STATE = "99_INBOX/CURATION/STATE/latest.json"
DEFAULT_HANDOFF = "99_INBOX/CURATION/HANDOFF/latest.json"
DEFAULT_MAX_HANDOFF = 50
MAX_HANDOFF = 200
MAX_STATE_ITEMS = 1000
MAX_STATE_BYTES = 192 * 1024
MAX_HANDOFF_BYTES = 96 * 1024
PACKAGE_ID_RE = re.compile(r"^[0-9a-f]{20}$")
STATES = ("pending", "analyzed", "rejected", "do_not_reprocess")
FINAL_REVISION_STATES = {"analyzed", "rejected"}
TERMINAL_STATES = {"do_not_reprocess"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *arguments], check=False, capture_output=True, text=True, encoding="utf-8"
    )


def read_json_text(text: str | None) -> dict[str, Any] | None:
    if not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return read_json_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None


def remote_json(remote_path: str) -> dict[str, Any] | None:
    result = run_rclone(["cat", remote_path, "--log-level", "ERROR"])
    if result.returncode != 0:
        return None
    return read_json_text(result.stdout)


def encoded_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def safe_version(value: Any) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 48:
        return None
    return value if re.fullmatch(r"[A-Za-z0-9._+-]+", value) else None


def safe_pointer(value: Any) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        return None
    value = value.strip("/")
    if not value or ".." in value.split("/"):
        return None
    return value


def ready_items(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    if payload.get("schema_version") != 1 or not isinstance(payload.get("items"), list):
        return None
    output: list[dict[str, Any]] = []
    for raw in payload["items"]:
        if not isinstance(raw, dict):
            continue
        package_id = raw.get("package_id")
        if not isinstance(package_id, str) or not PACKAGE_ID_RE.fullmatch(package_id):
            continue
        if not raw.get("eligible_for_analysis") or not raw.get("complete"):
            continue
        output.append(raw)
    return output


def revision_key(item: dict[str, Any]) -> str:
    material = {
        "package_id": item.get("package_id"),
        "kind": item.get("kind"),
        "pipeline_version": safe_version(item.get("pipeline_version")),
        "compactor_version": safe_version(item.get("compactor_version")),
        "evidence_bytes": int(item.get("evidence_bytes") or 0),
        "evidence_path": safe_pointer(item.get("evidence_path")),
        "detail_path": safe_pointer(item.get("detail_path")),
        "preview_path": safe_pointer(item.get("preview_path")),
    }
    data = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:20]


def parse_state(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return {}
    items = payload.get("items")
    if not isinstance(items, list):
        return {}
    output: dict[str, dict[str, Any]] = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        package_id = raw.get("package_id")
        state = raw.get("state")
        revision = raw.get("revision_key")
        if not isinstance(package_id, str) or not PACKAGE_ID_RE.fullmatch(package_id):
            continue
        if state not in STATES:
            continue
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{20}", revision):
            continue
        output[package_id] = {
            "package_id": package_id,
            "state": state,
            "revision_key": revision,
            "state_updated_at": raw.get("state_updated_at") if isinstance(raw.get("state_updated_at"), str) else None,
        }
    return output


def reconcile_state(
    ready: list[dict[str, Any]], existing: dict[str, dict[str, Any]], now: str
) -> tuple[dict[str, dict[str, Any]], int]:
    state = dict(existing)
    reopened = 0
    for item in ready:
        package_id = str(item["package_id"])
        revision = revision_key(item)
        previous = state.get(package_id)
        if previous is None:
            state[package_id] = {
                "package_id": package_id,
                "state": "pending",
                "revision_key": revision,
                "state_updated_at": now,
            }
            continue

        previous_state = previous["state"]
        previous_revision = previous["revision_key"]
        if previous_state in TERMINAL_STATES:
            continue
        if previous_revision == revision:
            continue
        if previous_state in FINAL_REVISION_STATES:
            reopened += 1
        state[package_id] = {
            "package_id": package_id,
            "state": "pending",
            "revision_key": revision,
            "state_updated_at": now,
        }
    return state, reopened


def parse_transitions(values: list[str]) -> list[tuple[str, str]]:
    transitions: list[tuple[str, str]] = []
    for value in values:
        if "=" not in value:
            raise ValueError("transition_format")
        package_id, state = value.split("=", 1)
        package_id = package_id.strip()
        state = state.strip()
        if not PACKAGE_ID_RE.fullmatch(package_id) or state not in STATES:
            raise ValueError("transition_invalid")
        transitions.append((package_id, state))
    return transitions


def apply_transitions(
    state: dict[str, dict[str, Any]],
    ready_by_id: dict[str, dict[str, Any]],
    transitions: list[tuple[str, str]],
    now: str,
) -> int:
    changed = 0
    for package_id, new_state in transitions:
        if package_id not in ready_by_id:
            raise ValueError("transition_package_not_ready")
        revision = revision_key(ready_by_id[package_id])
        previous = state.get(package_id)
        if previous and previous.get("state") == new_state and previous.get("revision_key") == revision:
            continue
        state[package_id] = {
            "package_id": package_id,
            "state": new_state,
            "revision_key": revision,
            "state_updated_at": now,
        }
        changed += 1
    return changed


def state_payload(state: dict[str, dict[str, Any]], now: str) -> dict[str, Any]:
    ordered = sorted(
        state.values(),
        key=lambda item: (str(item.get("state_updated_at") or ""), str(item.get("package_id") or "")),
        reverse=True,
    )[:MAX_STATE_ITEMS]
    counts = {name: 0 for name in STATES}
    for item in ordered:
        counts[item["state"]] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "state_version": STATE_VERSION,
        "generated_at": now,
        "counts": counts,
        "items": ordered,
    }


def handoff_payload(
    ready: list[dict[str, Any]], state: dict[str, dict[str, Any]], now: str, max_handoff: int
) -> dict[str, Any]:
    pending: list[dict[str, Any]] = []
    for item in ready:
        package_id = str(item["package_id"])
        state_item = state.get(package_id)
        if not state_item or state_item.get("state") != "pending":
            continue
        pending.append({
            "package_id": package_id,
            "revision_key": state_item["revision_key"],
            "kind": item.get("kind") if item.get("kind") in {"video", "image", "audio", "text", "document", "spreadsheet", "link", "unknown"} else "unknown",
            "pipeline_version": safe_version(item.get("pipeline_version")),
            "compactor_version": safe_version(item.get("compactor_version")),
            "evidence_bytes": max(0, int(item.get("evidence_bytes") or 0)),
            "evidence_path": safe_pointer(item.get("evidence_path")),
            "detail_path": safe_pointer(item.get("detail_path")),
            "preview_path": safe_pointer(item.get("preview_path")),
        })
    selected = pending[:max_handoff]
    return {
        "schema_version": SCHEMA_VERSION,
        "handoff_version": STATE_VERSION,
        "generated_at": now,
        "pending_total": len(pending),
        "selected": len(selected),
        "max_handoff": max_handoff,
        "items": selected,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def persist_remote(remote: str, relative: str, payload: dict[str, Any]) -> bool:
    with tempfile.TemporaryDirectory(prefix="tl-curation-") as temp_dir:
        local = Path(temp_dir) / "payload.json"
        write_json(local, payload)
        parent = relative.rsplit("/", 1)[0] if "/" in relative else ""
        if parent and run_rclone(["mkdir", join_remote(remote, parent), "--log-level", "ERROR"]).returncode != 0:
            return False
        return run_rclone([
            "copyto", str(local), join_remote(remote, relative),
            "--log-level", "ERROR", "--stats", "0",
        ]).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh privacy-safe curation state and pending handoff queue.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root-dir", type=Path)
    mode.add_argument("--remote")
    parser.add_argument("--ready-index", default=DEFAULT_READY_INDEX)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--handoff", default=DEFAULT_HANDOFF)
    parser.add_argument("--max-handoff", type=int, default=DEFAULT_MAX_HANDOFF)
    parser.add_argument("--transition", action="append", default=[])
    args = parser.parse_args()

    if not 1 <= args.max_handoff <= MAX_HANDOFF:
        parser.error(f"--max-handoff must be between 1 and {MAX_HANDOFF}")
    try:
        transitions = parse_transitions(args.transition)
    except ValueError as exc:
        print(f"curation_handoff_error code={exc}")
        return 2

    if args.remote and not shutil.which("rclone"):
        print("curation_handoff_error code=rclone_missing")
        return 2

    if args.root_dir is not None:
        root = args.root_dir.resolve()
        ready_payload = read_json_file(root / args.ready_index)
        current_state_payload = read_json_file(root / args.state)
    else:
        ready_payload = remote_json(join_remote(args.remote, args.ready_index))
        current_state_payload = remote_json(join_remote(args.remote, args.state))

    if ready_payload is None:
        print("curation_handoff_error code=ready_index_missing")
        return 2
    ready = ready_items(ready_payload)
    if ready is None:
        print("curation_handoff_error code=ready_index_invalid")
        return 2

    now = utc_now()
    state, reopened = reconcile_state(ready, parse_state(current_state_payload), now)
    ready_by_id = {str(item["package_id"]): item for item in ready}
    try:
        changed = apply_transitions(state, ready_by_id, transitions, now)
    except ValueError as exc:
        print(f"curation_handoff_error code={exc}")
        return 2

    state_out = state_payload(state, now)
    handoff_out = handoff_payload(ready, state, now, args.max_handoff)
    if encoded_size(state_out) > MAX_STATE_BYTES:
        print("curation_handoff_error code=state_budget_exceeded")
        return 2
    if encoded_size(handoff_out) > MAX_HANDOFF_BYTES:
        print("curation_handoff_error code=handoff_budget_exceeded")
        return 2

    if args.root_dir is not None:
        write_json(root / args.state, state_out)
        write_json(root / args.handoff, handoff_out)
        persisted = 1
    else:
        if not persist_remote(args.remote, args.state, state_out):
            print("curation_handoff_error code=state_upload_failed")
            return 2
        if not persist_remote(args.remote, args.handoff, handoff_out):
            print("curation_handoff_error code=handoff_upload_failed")
            return 2
        persisted = 1

    counts = state_out["counts"]
    print(
        "curation_handoff_ok "
        f"ready={len(ready)} pending={counts['pending']} analyzed={counts['analyzed']} "
        f"rejected={counts['rejected']} do_not_reprocess={counts['do_not_reprocess']} "
        f"handoff={handoff_out['selected']} reopened={reopened} transitions={changed} persisted={persisted}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
