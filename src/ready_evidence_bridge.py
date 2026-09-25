#!/usr/bin/env python3
"""Bridge pending READY_FOR_ANALYSIS packages into private evidence envelopes.

This is the mechanical convergence point between raw-file ingestion and the
candidate validation system. It consumes the privacy-safe curation handoff,
reads only each package's bounded evidence-summary.json, strips provider/source
identifiers, and writes a private FILE_EVIDENCE envelope plus a current index.

An evidence envelope is NOT a semantic candidate and is never canonical
knowledge. This stage performs no model call, no web research, no curation-state
transition and no write to 00_LIBRARY.
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
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
BRIDGE_VERSION = "0.1.0"
DEFAULT_HANDOFF = "99_INBOX/CURATION/HANDOFF/latest.json"
DEFAULT_DESTINATION = "99_INBOX/CANDIDATES/FILE_EVIDENCE"
DEFAULT_READY = "99_INBOX/READY_FOR_ANALYSIS"
DEFAULT_MAX_ITEMS = 50
MAX_ITEMS = 200
MAX_EVIDENCE_BYTES = 16 * 1024
MAX_ENVELOPE_BYTES = 24 * 1024
PACKAGE_ID_RE = re.compile(r"^[0-9a-f]{20}$")
REVISION_RE = re.compile(r"^[0-9a-f]{20}$")
KINDS = {"video", "image", "audio", "text", "document", "spreadsheet", "link", "unknown"}
SEMANTIC_TOP_LEVEL = {
    "media",
    "quality",
    "evidence",
    "warnings",
    "content",
    "extraction",
    "image",
    "audio",
    "spreadsheet",
    "link",
    "network",
}


def compact_json(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def read_json_bytes(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read_local(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["rclone", *arguments], check=False, capture_output=True)


def remote_bytes(remote: str, relative: str) -> bytes | None:
    result = run_rclone(["cat", join_remote(remote, relative), "--log-level", "ERROR"])
    return result.stdout if result.returncode == 0 else None


def safe_pointer(value: Any) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        return None
    value = value.replace("\\", "/").strip("/")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts:
        return None
    return str(pure)


def expected_evidence_path(package_id: str) -> str:
    return str(PurePosixPath(DEFAULT_READY) / package_id / "evidence-summary.json")


def parse_handoff(payload: dict[str, Any] | None, max_items: int) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return [], ["handoff_invalid"]
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > MAX_ITEMS:
        return [], ["handoff_items_invalid"]

    output: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for position, raw in enumerate(raw_items[:max_items]):
        if not isinstance(raw, dict):
            errors.append(f"item_{position}_invalid")
            continue
        package_id = raw.get("package_id")
        revision = raw.get("revision_key")
        kind = raw.get("kind")
        evidence_path = safe_pointer(raw.get("evidence_path"))
        if not isinstance(package_id, str) or not PACKAGE_ID_RE.fullmatch(package_id):
            errors.append(f"item_{position}_package_id")
            continue
        if package_id in seen:
            errors.append(f"item_{position}_duplicate_package")
            continue
        seen.add(package_id)
        if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
            errors.append(f"item_{position}_revision")
            continue
        if kind not in KINDS:
            errors.append(f"item_{position}_kind")
            continue
        if evidence_path != expected_evidence_path(package_id):
            errors.append(f"item_{position}_evidence_path")
            continue
        detail_path = safe_pointer(raw.get("detail_path")) if raw.get("detail_path") is not None else None
        preview_path = safe_pointer(raw.get("preview_path")) if raw.get("preview_path") is not None else None
        output.append(
            {
                "package_id": package_id,
                "revision_key": revision,
                "kind": str(kind),
                "evidence_path": evidence_path,
                "detail_path": detail_path,
                "preview_path": preview_path,
            }
        )
    return output, sorted(set(errors))


def bounded_json_value(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:8000]
    if isinstance(value, list):
        return [bounded_json_value(item, depth + 1) for item in value[:100]]
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in list(value.items())[:100]:
            if isinstance(key, str) and 1 <= len(key) <= 128:
                output[key] = bounded_json_value(item, depth + 1)
        return output
    return None


def semantic_summary(summary: dict[str, Any] | None) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(summary, dict) or summary.get("schema_version") != 1:
        return None, ["evidence_summary_invalid"]
    payload: dict[str, Any] = {"schema_version": 1}
    summary_version = summary.get("summary_version") or summary.get("compactor_version")
    if isinstance(summary_version, str) and 1 <= len(summary_version) <= 64:
        payload["summary_version"] = summary_version
    for key in SEMANTIC_TOP_LEVEL:
        if key in summary:
            payload[key] = bounded_json_value(summary[key])
    if len(payload) <= 2 and "evidence" not in payload:
        return None, ["evidence_summary_empty"]
    return payload, []


def build_envelope(
    item: dict[str, Any],
    evidence_bytes: bytes,
) -> tuple[dict[str, Any] | None, list[str]]:
    if len(evidence_bytes) > MAX_EVIDENCE_BYTES:
        return None, ["evidence_summary_too_large"]
    summary = read_json_bytes(evidence_bytes)
    semantic, errors = semantic_summary(summary)
    if errors or semantic is None:
        return None, errors or ["evidence_summary_invalid"]
    evidence_sha = hashlib.sha256(evidence_bytes).hexdigest()
    package_id = str(item["package_id"])
    revision = str(item["revision_key"])
    envelope_id = hashlib.sha256(
        f"file-evidence-v1|{package_id}|{revision}|{evidence_sha}".encode("utf-8")
    ).hexdigest()[:20]
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "bridge_version": BRIDGE_VERSION,
        "type": "FILE_EVIDENCE_ENVELOPE",
        "state": "READY_FOR_SEMANTIC_EXTRACTION",
        "envelope_id": envelope_id,
        "package_id": package_id,
        "revision_key": revision,
        "kind": item["kind"],
        "evidence_summary_sha256": evidence_sha,
        "pointers": {
            "evidence": item["evidence_path"],
            "detail": item.get("detail_path"),
            "preview": item.get("preview_path"),
        },
        "semantic_summary": semantic,
        "candidate_created": False,
        "curation_transition_performed": False,
        "canonical_write_performed": False,
        "paid_model_used": False,
    }
    if len(compact_json(envelope)) > MAX_ENVELOPE_BYTES:
        return None, ["envelope_budget_exceeded"]
    return envelope, []


def write_local(path: Path, raw: bytes) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path.read_bytes() == raw
    except OSError:
        return False


def write_remote(remote: str, relative: str, raw: bytes) -> bool:
    parent = str(PurePosixPath(relative).parent)
    if parent not in {"", "."}:
        if run_rclone(["mkdir", join_remote(remote, parent), "--log-level", "ERROR"]).returncode != 0:
            return False
    with tempfile.NamedTemporaryFile(prefix="tl-file-evidence-", delete=False) as handle:
        handle.write(raw)
        local_name = handle.name
    try:
        result = run_rclone([
            "copyto", local_name, join_remote(remote, relative),
            "--log-level", "ERROR", "--stats", "0",
        ])
        if result.returncode != 0:
            return False
        written = remote_bytes(remote, relative)
        return written == raw
    finally:
        try:
            Path(local_name).unlink()
        except OSError:
            pass


def build_index(prepared: list[dict[str, Any]], held: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "bridge_version": BRIDGE_VERSION,
        "type": "FILE_EVIDENCE_INDEX",
        "active": len(prepared),
        "held": len(held),
        "items": [
            {
                "package_id": item["package_id"],
                "revision_key": item["revision_key"],
                "kind": item["kind"],
                "envelope_id": item["envelope_id"],
                "path": str(PurePosixPath(DEFAULT_DESTINATION) / f"{item['package_id']}.json"),
                "evidence_summary_sha256": item["evidence_summary_sha256"],
            }
            for item in prepared
        ],
        "held_items": held,
        "candidate_created": False,
        "curation_transition_performed": False,
        "canonical_write_performed": False,
        "paid_model_used": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge pending READY evidence into private non-candidate envelopes.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root-dir", type=Path)
    mode.add_argument("--remote")
    parser.add_argument("--handoff", default=DEFAULT_HANDOFF)
    parser.add_argument("--destination", default=DEFAULT_DESTINATION)
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    args = parser.parse_args()

    if not 1 <= args.max_items <= MAX_ITEMS:
        parser.error(f"--max-items must be between 1 and {MAX_ITEMS}")
    if args.remote and not shutil.which("rclone"):
        print("ready_evidence_bridge_error code=rclone_missing candidate_write=0 canonical_write=0")
        return 2

    root = args.root_dir.resolve() if args.root_dir is not None else None
    handoff_raw = read_local(root / args.handoff) if root is not None else remote_bytes(str(args.remote), args.handoff)
    handoff = read_json_bytes(handoff_raw)
    items, handoff_errors = parse_handoff(handoff, args.max_items)
    if handoff_errors:
        print(f"ready_evidence_bridge_error codes={','.join(handoff_errors)} candidate_write=0 canonical_write=0")
        return 2

    prepared: list[dict[str, Any]] = []
    held: list[dict[str, str]] = []
    for item in items:
        evidence_raw = read_local(root / item["evidence_path"]) if root is not None else remote_bytes(str(args.remote), item["evidence_path"])
        if evidence_raw is None:
            held.append({"package_id": item["package_id"], "reason": "evidence_missing"})
            continue
        envelope, errors = build_envelope(item, evidence_raw)
        if errors or envelope is None:
            held.append({"package_id": item["package_id"], "reason": (errors or ["envelope_invalid"])[0]})
            continue
        relative = str(PurePosixPath(args.destination) / f"{item['package_id']}.json")
        raw = compact_json(envelope)
        ok = write_local(root / relative, raw) if root is not None else write_remote(str(args.remote), relative, raw)
        if not ok:
            print("ready_evidence_bridge_error code=envelope_write_failed candidate_write=0 canonical_write=0")
            return 2
        prepared.append(envelope)

    index = build_index(prepared, held)
    index_rel = str(PurePosixPath(args.destination) / "index.json")
    index_raw = compact_json(index)
    ok = write_local(root / index_rel, index_raw) if root is not None else write_remote(str(args.remote), index_rel, index_raw)
    if not ok:
        print("ready_evidence_bridge_error code=index_write_failed candidate_write=0 canonical_write=0")
        return 2

    print(
        "ready_evidence_bridge_ok "
        f"seen={len(items)} active={len(prepared)} held={len(held)} "
        "candidate_write=0 curation_transition=0 canonical_write=0 paid_model=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
