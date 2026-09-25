#!/usr/bin/env python3
"""Run local semantic planning over active FILE_EVIDENCE envelopes.

This batch is intentionally manual/local. It reads the current private
FILE_EVIDENCE index, verifies each envelope against the indexed revision/hash,
asks loopback-only Ollama for a semantic plan, and stores plans privately under
FILE_EVIDENCE/SEMANTIC_PLANS. It never creates candidate Markdown, never changes
curation status, and never writes to 00_LIBRARY.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_semantic_local import endpoint_is_loopback
from file_evidence_semantic_plan import (
    build_plan,
    build_prompt,
    call_ollama,
    envelope_binding,
)

SCHEMA_VERSION = 1
BATCH_VERSION = "0.1.1"
DEFAULT_ROOT = "99_INBOX/CANDIDATES/FILE_EVIDENCE"
DEFAULT_INDEX = "index.json"
DEFAULT_PLANS = "SEMANTIC_PLANS"
DEFAULT_STATE = "semantic-batch-state.json"
DEFAULT_MASTER = "00_LIBRARY/MASTER_INDEX.md"
MAX_ITEMS = 20


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["rclone", *arguments], check=False, capture_output=True)


def remote_bytes(remote: str, relative: str) -> bytes | None:
    result = run_rclone(["cat", join_remote(remote, relative), "--log-level", "ERROR"])
    return result.stdout if result.returncode == 0 else None


def remote_text(remote: str, relative: str) -> str | None:
    raw = remote_bytes(remote, relative)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def remote_json(remote: str, relative: str) -> dict[str, Any] | None:
    text = remote_text(remote, relative)
    if text is None:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def upload_json(remote: str, relative: str, payload: dict[str, Any]) -> bool:
    parent = str(PurePosixPath(relative).parent)
    if parent not in {"", "."}:
        if run_rclone(["mkdir", join_remote(remote, parent), "--log-level", "ERROR"]).returncode != 0:
            return False
    with tempfile.NamedTemporaryFile(prefix="tl-file-evidence-plan-", suffix=".json", delete=False) as handle:
        handle.write((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        local_name = handle.name
    try:
        result = run_rclone([
            "copyto", local_name, join_remote(remote, relative),
            "--log-level", "ERROR", "--stats", "0",
        ])
        return result.returncode == 0
    finally:
        try:
            Path(local_name).unlink()
        except OSError:
            pass


def active_items(
    index: dict[str, Any] | None,
    max_items: int,
    root: str = DEFAULT_ROOT,
) -> tuple[list[dict[str, str]], list[str]]:
    if not isinstance(index, dict) or index.get("schema_version") != 1 or index.get("type") != "FILE_EVIDENCE_INDEX":
        return [], ["index_invalid"]
    raw_items = index.get("items")
    if not isinstance(raw_items, list):
        return [], ["index_items_invalid"]
    output: list[dict[str, str]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for position, raw in enumerate(raw_items[:max_items]):
        if not isinstance(raw, dict):
            errors.append(f"item_{position}_invalid")
            continue
        values = {
            "package_id": raw.get("package_id"),
            "revision_key": raw.get("revision_key"),
            "envelope_id": raw.get("envelope_id"),
            "evidence_summary_sha256": raw.get("evidence_summary_sha256"),
            "path": raw.get("path"),
        }
        if not all(isinstance(value, str) and value for value in values.values()):
            errors.append(f"item_{position}_fields")
            continue
        package_id = str(values["package_id"])
        if package_id in seen:
            errors.append(f"item_{position}_duplicate_package")
            continue
        seen.add(package_id)
        expected = str(PurePosixPath(root) / f"{package_id}.json")
        if values["path"] != expected:
            errors.append(f"item_{position}_path")
            continue
        output.append({key: str(value) for key, value in values.items()})
    return output, sorted(set(errors))


def indexed_binding_matches(item: dict[str, str], envelope: dict[str, Any] | None) -> bool:
    binding, errors = envelope_binding(envelope)
    if errors or binding is None:
        return False
    return all(
        binding[key] == item[key]
        for key in ("package_id", "revision_key", "envelope_id", "evidence_summary_sha256")
    )


def fallback_payload(reason: str) -> dict[str, Any]:
    return {
        "outcome": "NEEDS_REVIEW",
        "rationale": f"Automated local file-evidence review could not complete safely: {reason}.",
        "candidates": [],
    }


def build_batch_state(
    plans: list[dict[str, Any]],
    failed: int,
    held: list[dict[str, str]],
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for plan in plans:
        outcome = str(plan.get("outcome") or "UNKNOWN")
        counts[outcome] = counts.get(outcome, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_version": BATCH_VERSION,
        "processed": len(plans),
        "held": len(held),
        "held_items": held,
        "fail_closed_items": failed,
        "outcome_counts": counts,
        "candidate_write_performed": False,
        "curation_transition_performed": False,
        "canonical_write_performed": False,
        "paid_model_used": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run manual local semantic planning for active FILE_EVIDENCE envelopes.")
    parser.add_argument("--remote", default="tl:")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--model", required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--max-items", type=int, default=MAX_ITEMS)
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("file_evidence_semantic_batch_error code=rclone_missing candidate_write=0 canonical_write=0")
        return 2
    if not endpoint_is_loopback(args.endpoint):
        print("file_evidence_semantic_batch_error code=non_loopback_model_endpoint candidate_write=0 canonical_write=0")
        return 2
    max_items = max(1, min(int(args.max_items), MAX_ITEMS))
    index = remote_json(args.remote, str(PurePosixPath(args.root) / DEFAULT_INDEX))
    items, index_errors = active_items(index, max_items, args.root)
    if index_errors:
        print(f"file_evidence_semantic_batch_error codes={','.join(index_errors)} candidate_write=0 canonical_write=0")
        return 2
    master_text = remote_text(args.remote, DEFAULT_MASTER)
    if master_text is None:
        print("file_evidence_semantic_batch_error code=master_index_missing candidate_write=0 canonical_write=0")
        return 2

    plans: list[dict[str, Any]] = []
    held: list[dict[str, str]] = []
    failed = 0
    for item in items:
        envelope = remote_json(args.remote, item["path"])
        if not indexed_binding_matches(item, envelope):
            held.append({"package_id": item["package_id"], "reason": "indexed_envelope_binding_mismatch"})
            continue

        binding, _ = envelope_binding(envelope)
        assert binding is not None
        model_payload = call_ollama(
            args.endpoint,
            args.model,
            build_prompt(binding, master_text),
            max(10.0, min(float(args.timeout), 600.0)),
        )
        if model_payload is None:
            model_payload = fallback_payload("local model review failed")
            failed += 1

        plan, errors = build_plan(envelope, master_text, model_payload)
        if errors or plan is None:
            # A malformed model answer may be converted into NEEDS_REVIEW only
            # after the current envelope binding has already been verified.
            fallback = fallback_payload("model output violated semantic plan contract")
            plan, fallback_errors = build_plan(envelope, master_text, fallback)
            failed += 1
            if fallback_errors or plan is None:
                held.append({"package_id": item["package_id"], "reason": "fallback_plan_failed"})
                continue
        relative = str(PurePosixPath(args.root) / DEFAULT_PLANS / f"{item['package_id']}.json")
        if not upload_json(args.remote, relative, plan):
            print("file_evidence_semantic_batch_error code=plan_write_failed candidate_write=0 canonical_write=0")
            return 2
        plans.append(plan)

    state = build_batch_state(plans, failed, held)
    state_rel = str(PurePosixPath(args.root) / DEFAULT_STATE)
    if not upload_json(args.remote, state_rel, state):
        print("file_evidence_semantic_batch_error code=state_write_failed candidate_write=0 canonical_write=0")
        return 2
    print(
        "file_evidence_semantic_batch_ok "
        f"processed={len(plans)} held={len(held)} fail_closed={failed} outcomes={len(state['outcome_counts'])} "
        "candidate_write=0 curation_transition=0 canonical_write=0 paid_model=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
