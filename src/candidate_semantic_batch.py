#!/usr/bin/env python3
"""Orchestrate adaptive candidate semantic review with local Ollama only.

This is the first executable zero-additional-cost semantic path for chat-research
candidates. It reads private candidate state through rclone, probes only URLs
explicitly present in each candidate, asks a loopback-only Ollama model for a
strict review, validates decisions against the current candidate hashes and
persists private review/decision state.

It never writes to 00_LIBRARY and never calls a paid/remote model endpoint.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_batch_plan import build_plan
from candidate_semantic_contract import SEMANTIC_CONTRACT_VERSION, validate_semantic_payload
from candidate_semantic_local import endpoint_is_loopback, review_candidate
from candidate_source_probe import build_probe

SCHEMA_VERSION = 1
BATCH_VERSION = "0.3.0"
DEFAULT_ROOT = "99_INBOX/CANDIDATES"
DEFAULT_INDEX = "index.json"
DEFAULT_PROPOSALS = "validation-proposals.json"
DEFAULT_MASTER = "00_LIBRARY/MASTER_INDEX.md"
DEFAULT_REVIEWS = "SEMANTIC_REVIEWS"
DEFAULT_DECISIONS = "semantic-decisions.json"
DEFAULT_STATE = "semantic-batch-state.json"


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


def normalize_candidate_text(text: str) -> str:
    """Match the queue's universal-newline semantics across Windows/Linux reads."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


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
        mkdir = run_rclone(["mkdir", join_remote(remote, parent), "--log-level", "ERROR"])
        if mkdir.returncode != 0:
            return False
    with tempfile.TemporaryDirectory(prefix="tl-semantic-batch-") as temp_dir:
        path = Path(temp_dir) / PurePosixPath(relative).name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result = run_rclone([
            "copyto",
            str(path),
            join_remote(remote, relative),
            "--log-level",
            "ERROR",
            "--stats",
            "0",
        ])
        return result.returncode == 0


def proposal_map(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    if not isinstance(payload, dict):
        return output
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return output
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        candidate_id = raw.get("candidate_id")
        if isinstance(candidate_id, str):
            output[candidate_id] = raw
    return output


def index_map(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    if not isinstance(payload, dict):
        return output
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return output
    for raw in entries:
        if not isinstance(raw, dict) or raw.get("valid") is not True:
            continue
        candidate_id = raw.get("candidate_id")
        if isinstance(candidate_id, str):
            output[candidate_id] = raw
    return output


def fallback_review(candidate_id: str, candidate_sha: str, reason: str) -> tuple[dict[str, Any], dict[str, Any]]:
    rich = {
        "schema_version": SCHEMA_VERSION,
        "adapter_version": "fallback",
        "candidate_id": candidate_id,
        "candidate_sha256": candidate_sha,
        "source_probe_successful": 0,
        "model_review": None,
        "final_review": {
            "decision": "NEEDS_REVIEW",
            "evidence_level": "LOW",
            "proposed_type": None,
            "proposed_status": None,
            "canonical_match_path": None,
            "rationale": "Automated semantic validation could not complete safely.",
        },
        "policy_reasons": [reason],
        "canonical_write_performed": False,
    }
    decision = {
        "candidate_id": candidate_id,
        "candidate_sha256": candidate_sha,
        "decision": "NEEDS_REVIEW",
        "evidence_level": "LOW",
    }
    return rich, decision


def review_one(
    *,
    candidate_id: str,
    candidate_sha: str,
    candidate_text: str,
    master_text: str,
    model: str,
    endpoint: str,
    timeout: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate_text = normalize_candidate_text(candidate_text)
    actual_sha = hashlib.sha256(candidate_text.encode("utf-8")).hexdigest()
    if actual_sha != candidate_sha or actual_sha[:20] != candidate_id:
        return fallback_review(candidate_id, candidate_sha, "candidate_revision_changed")

    probe = build_probe(candidate_text)
    if probe.get("source_count", 0) == 0:
        rich, decision = fallback_review(candidate_id, candidate_sha, "no_explicit_sources")
        rich["source_probe"] = {
            "source_count": 0,
            "successful_sources": 0,
            "blocked_sources": 0,
            "failed_sources": 0,
        }
        return rich, decision

    try:
        rich, decision_payload, errors = review_candidate(
            candidate_text,
            probe,
            master_text,
            model=model,
            endpoint=endpoint,
            timeout=timeout,
        )
    except ValueError:
        rich, decision = fallback_review(candidate_id, candidate_sha, "local_model_review_failed")
        rich["source_probe"] = {
            "source_count": int(probe.get("source_count", 0)),
            "successful_sources": int(probe.get("successful_sources", 0)),
            "blocked_sources": int(probe.get("blocked_sources", 0)),
            "failed_sources": int(probe.get("failed_sources", 0)),
        }
        return rich, decision

    if errors or rich is None or decision_payload is None:
        rich, decision = fallback_review(candidate_id, candidate_sha, "local_model_review_failed")
        rich["source_probe"] = {
            "source_count": int(probe.get("source_count", 0)),
            "successful_sources": int(probe.get("successful_sources", 0)),
            "blocked_sources": int(probe.get("blocked_sources", 0)),
            "failed_sources": int(probe.get("failed_sources", 0)),
        }
        return rich, decision

    rich["source_probe"] = probe
    return rich, dict(decision_payload["items"][0])


def build_state(plan: dict[str, Any], decisions: list[dict[str, Any]], failures: int) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in decisions:
        decision = str(item.get("decision") or "UNKNOWN")
        counts[decision] = counts.get(decision, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_version": BATCH_VERSION,
        "plan_state": plan.get("state"),
        "plan_reason": plan.get("reason"),
        "selected": len(decisions),
        "fail_closed_items": failures,
        "decision_counts": counts,
        "canonical_write_performed": False,
        "paid_model_used": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run adaptive chat-candidate semantic validation using local Ollama.")
    parser.add_argument("--remote", default="tl:")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--model", required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--volume-threshold", type=int, default=5)
    parser.add_argument("--max-wait-days", type=int, default=30)
    parser.add_argument("--max-batch", type=int, default=20)
    parser.add_argument("--force", action="store_true", help="Run pending READY_FOR_SEMANTIC candidates now; manual testing only.")
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("candidate_semantic_batch_error code=rclone_missing canonical_write=0")
        return 2
    if not endpoint_is_loopback(args.endpoint):
        print("candidate_semantic_batch_error code=non_loopback_model_endpoint canonical_write=0")
        return 2

    index_rel = str(PurePosixPath(args.root) / DEFAULT_INDEX)
    proposal_rel = str(PurePosixPath(args.root) / DEFAULT_PROPOSALS)
    index = remote_json(args.remote, index_rel)
    proposals = remote_json(args.remote, proposal_rel)
    master_text = remote_text(args.remote, DEFAULT_MASTER)
    if index is None or proposals is None or master_text is None:
        print("candidate_semantic_batch_error code=private_state_missing canonical_write=0")
        return 2

    plan = build_plan(
        index,
        proposals,
        volume_threshold=args.volume_threshold,
        max_wait_days=args.max_wait_days,
        max_batch=args.max_batch,
    )
    if plan.get("state") == "HOLD":
        print("candidate_semantic_batch_error code=adaptive_plan_hold canonical_write=0")
        return 2

    selected = list(plan.get("selected_candidate_ids") or [])
    if args.force:
        ready = [
            raw.get("candidate_id")
            for raw in (proposals.get("entries") or [])
            if isinstance(raw, dict)
            and raw.get("proposal") == "READY_FOR_SEMANTIC"
            and isinstance(raw.get("candidate_id"), str)
        ]
        selected = ready[: max(1, min(int(args.max_batch), 20))]
        plan = dict(plan)
        plan["state"] = "DUE" if selected else "IDLE"
        plan["reason"] = "manual_force" if selected else "no_semantic_work"

    if not selected:
        print(
            "candidate_semantic_batch_ok "
            f"state={plan.get('state')} selected=0 canonical_write=0 paid_model=0"
        )
        return 0

    current = index_map(index)
    proposals_by_id = proposal_map(proposals)
    decisions: list[dict[str, Any]] = []
    rich_reviews: list[dict[str, Any]] = []
    fail_closed = 0

    for candidate_id in selected:
        entry = current.get(candidate_id)
        proposal = proposals_by_id.get(candidate_id)
        if entry is None or proposal is None or proposal.get("proposal") != "READY_FOR_SEMANTIC":
            continue
        candidate_sha = entry.get("sha256")
        relative_path = entry.get("path")
        if not isinstance(candidate_sha, str) or not isinstance(relative_path, str):
            continue
        candidate_rel = str(PurePosixPath(args.root) / relative_path)
        candidate_text = remote_text(args.remote, candidate_rel)
        if candidate_text is None:
            rich, decision = fallback_review(candidate_id, candidate_sha, "candidate_read_failed")
        else:
            rich, decision = review_one(
                candidate_id=candidate_id,
                candidate_sha=candidate_sha,
                candidate_text=candidate_text,
                master_text=master_text,
                model=args.model,
                endpoint=args.endpoint,
                timeout=max(10.0, min(float(args.timeout), 600.0)),
            )
        if decision["decision"] == "NEEDS_REVIEW" and rich.get("policy_reasons"):
            fail_closed += 1
        decisions.append(decision)
        rich_reviews.append(rich)

    semantic_payload = {
        "schema_version": SCHEMA_VERSION,
        "semantic_contract_version": SEMANTIC_CONTRACT_VERSION,
        "items": decisions,
    }
    accepted, contract_errors = validate_semantic_payload(semantic_payload, index)
    if contract_errors or len(accepted) != len(decisions):
        print("candidate_semantic_batch_error code=semantic_contract_failed canonical_write=0")
        return 2

    for review in rich_reviews:
        candidate_id = review.get("candidate_id")
        if not isinstance(candidate_id, str):
            continue
        review_rel = str(PurePosixPath(args.root) / DEFAULT_REVIEWS / f"{candidate_id}.json")
        if not upload_json(args.remote, review_rel, review):
            print("candidate_semantic_batch_error code=review_write_failed canonical_write=0")
            return 2

    decisions_rel = str(PurePosixPath(args.root) / DEFAULT_DECISIONS)
    if not upload_json(args.remote, decisions_rel, semantic_payload):
        print("candidate_semantic_batch_error code=decision_write_failed canonical_write=0")
        return 2

    state = build_state(plan, decisions, fail_closed)
    state_rel = str(PurePosixPath(args.root) / DEFAULT_STATE)
    if not upload_json(args.remote, state_rel, state):
        print("candidate_semantic_batch_error code=state_write_failed canonical_write=0")
        return 2

    print(
        "candidate_semantic_batch_ok "
        f"state={plan.get('state')} selected={len(decisions)} fail_closed={fail_closed} "
        f"canonical_write=0 paid_model=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
