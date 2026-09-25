#!/usr/bin/env python3
"""Run the zero-cost local candidate review cycle without canonical publication.

This is the operator-friendly wrapper for chat-research candidates. It chains the
already isolated stages in the correct order:

candidate_queue -> candidate_validator -> candidate_semantic_batch
-> candidate_resolution -> candidate_finalize_local

The wrapper may move candidate lifecycle files and write private review/draft/
dry-run state, but none of the invoked stages can write to 00_LIBRARY. There is
no candidate canonical publication step here.

Use --force-semantic only for an explicit/manual review such as validating the
first small candidate batch before adaptive volume/age thresholds are reached.
"""

from __future__ import annotations

from rclone_paths import join_remote

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from candidate_batch_plan import build_plan

CYCLE_VERSION = "0.2.0"
DEFAULT_ROOT = "99_INBOX/CANDIDATES"
DEFAULT_INDEX = "index.json"
DEFAULT_PROPOSALS = "validation-proposals.json"
DEFAULT_DECISIONS = "semantic-decisions.json"
DEFAULT_VOLUME_THRESHOLD = 5
DEFAULT_MAX_WAIT_DAYS = 30
DEFAULT_MAX_BATCH = 20


def run_command(command: list[str]) -> int:
    result = subprocess.run(command, check=False)
    return int(result.returncode)


def run_rclone(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["rclone", *arguments], check=False, capture_output=True)


def remote_bytes(remote: str, relative: str) -> bytes | None:
    result = run_rclone(["cat", join_remote(remote, relative), "--log-level", "ERROR"])
    return result.stdout if result.returncode == 0 else None


def remote_json(remote: str, relative: str) -> dict[str, Any] | None:
    raw = remote_bytes(remote, relative)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def selected_from_state(payload: dict[str, Any] | None) -> int | None:
    """Compatibility helper retained for smoke coverage of semantic state shape."""
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    value = payload.get("selected")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def forced_ready_ids(proposals: dict[str, Any] | None, max_batch: int) -> list[str]:
    if not isinstance(proposals, dict) or not isinstance(proposals.get("entries"), list):
        return []
    ready: list[str] = []
    for raw in proposals["entries"]:
        if not isinstance(raw, dict) or raw.get("proposal") != "READY_FOR_SEMANTIC":
            continue
        candidate_id = raw.get("candidate_id")
        if isinstance(candidate_id, str):
            ready.append(candidate_id)
    return ready[:max_batch]


def expected_selected_ids(
    index: dict[str, Any] | None,
    proposals: dict[str, Any] | None,
    *,
    force_semantic: bool,
    volume_threshold: int,
    max_wait_days: int,
    max_batch: int,
) -> tuple[list[str] | None, str | None]:
    if not isinstance(index, dict) or not isinstance(proposals, dict):
        return None, "private_state_missing"
    if force_semantic:
        return forced_ready_ids(proposals, max_batch), None
    plan = build_plan(
        index,
        proposals,
        volume_threshold=volume_threshold,
        max_wait_days=max_wait_days,
        max_batch=max_batch,
    )
    if plan.get("state") == "HOLD":
        return None, "adaptive_plan_hold"
    selected = plan.get("selected_candidate_ids")
    if not isinstance(selected, list) or any(not isinstance(value, str) for value in selected):
        return None, "adaptive_plan_invalid"
    return list(selected), None


def decision_ids(payload: dict[str, Any] | None) -> list[str] | None:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    items = payload.get("items")
    if not isinstance(items, list):
        return None
    output: list[str] = []
    for raw in items:
        if not isinstance(raw, dict) or not isinstance(raw.get("candidate_id"), str):
            return None
        output.append(raw["candidate_id"])
    return output


def stage_commands(
    *,
    python_exe: str,
    remote: str,
    root: str,
    model: str,
    endpoint: str,
    timeout: float,
    force_semantic: bool,
    volume_threshold: int = DEFAULT_VOLUME_THRESHOLD,
    max_wait_days: int = DEFAULT_MAX_WAIT_DAYS,
    max_batch: int = DEFAULT_MAX_BATCH,
) -> tuple[list[str], list[str], list[str], list[str]]:
    queue = [
        python_exe,
        "src/candidate_queue.py",
        "--remote",
        remote,
        "--root",
        root,
    ]
    validator = [
        python_exe,
        "src/candidate_validator.py",
        "--remote",
        remote,
        "--candidate-root",
        root,
    ]
    semantic = [
        python_exe,
        "src/candidate_semantic_batch.py",
        "--remote",
        remote,
        "--root",
        root,
        "--model",
        model,
        "--endpoint",
        endpoint,
        "--timeout",
        str(timeout),
        "--volume-threshold",
        str(volume_threshold),
        "--max-wait-days",
        str(max_wait_days),
        "--max-batch",
        str(max_batch),
    ]
    if force_semantic:
        semantic.append("--force")
    finalize = [
        python_exe,
        "src/candidate_finalize_local.py",
        "--remote",
        remote,
        "--root",
        root,
        "--model",
        model,
        "--endpoint",
        endpoint,
        "--timeout",
        str(timeout),
        "--max-items",
        str(max_batch),
    ]
    return queue, validator, semantic, finalize


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the local candidate validation/curation cycle without canonical publication."
    )
    parser.add_argument("--remote", default="tl:")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--model", required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--volume-threshold", type=int, default=DEFAULT_VOLUME_THRESHOLD)
    parser.add_argument("--max-wait-days", type=int, default=DEFAULT_MAX_WAIT_DAYS)
    parser.add_argument("--max-batch", type=int, default=DEFAULT_MAX_BATCH)
    parser.add_argument(
        "--force-semantic",
        action="store_true",
        help="Explicit manual test: review READY_FOR_SEMANTIC candidates now instead of waiting for adaptive thresholds.",
    )
    args = parser.parse_args()

    if not shutil.which("rclone"):
        print("candidate_local_cycle_error stage=preflight code=rclone_missing canonical_write=0")
        return 2

    volume_threshold = max(1, min(int(args.volume_threshold), 1000))
    max_wait_days = max(1, min(int(args.max_wait_days), 3650))
    max_batch = max(1, min(int(args.max_batch), 20))
    python_exe = sys.executable or "python3"
    timeout = max(10.0, min(float(args.timeout), 600.0))
    queue_cmd, validator_cmd, semantic_cmd, finalize_cmd = stage_commands(
        python_exe=python_exe,
        remote=args.remote,
        root=args.root,
        model=args.model,
        endpoint=args.endpoint,
        timeout=timeout,
        force_semantic=bool(args.force_semantic),
        volume_threshold=volume_threshold,
        max_wait_days=max_wait_days,
        max_batch=max_batch,
    )

    for stage, command in (("queue", queue_cmd), ("validator", validator_cmd)):
        code = run_command(command)
        if code != 0:
            print(
                f"candidate_local_cycle_error stage={stage} code=stage_failed "
                "canonical_write=0"
            )
            return code

    index_rel = str(PurePosixPath(args.root) / DEFAULT_INDEX)
    proposals_rel = str(PurePosixPath(args.root) / DEFAULT_PROPOSALS)
    index = remote_json(args.remote, index_rel)
    proposals = remote_json(args.remote, proposals_rel)
    expected_ids, plan_error = expected_selected_ids(
        index,
        proposals,
        force_semantic=bool(args.force_semantic),
        volume_threshold=volume_threshold,
        max_wait_days=max_wait_days,
        max_batch=max_batch,
    )
    if plan_error or expected_ids is None:
        print(
            f"candidate_local_cycle_error stage=plan code={plan_error or 'plan_failed'} "
            "canonical_write=0"
        )
        return 2
    if not expected_ids:
        print(
            "candidate_local_cycle_ok state=idle selected=0 resolved=0 finalized=0 "
            f"cycle_version={CYCLE_VERSION} canonical_write=0 paid_model=0"
        )
        return 0

    code = run_command(semantic_cmd)
    if code != 0:
        print(
            "candidate_local_cycle_error stage=semantic code=stage_failed canonical_write=0"
        )
        return code

    decisions_rel = str(PurePosixPath(args.root) / DEFAULT_DECISIONS)
    decisions = remote_json(args.remote, decisions_rel)
    actual_ids = decision_ids(decisions)
    if actual_ids is None or set(actual_ids) != set(expected_ids) or len(actual_ids) != len(expected_ids):
        print(
            "candidate_local_cycle_error stage=semantic code=decision_batch_mismatch "
            "canonical_write=0"
        )
        return 2

    index_raw = remote_bytes(args.remote, index_rel)
    decisions_raw = remote_bytes(args.remote, decisions_rel)
    if index_raw is None or decisions_raw is None:
        print(
            "candidate_local_cycle_error stage=resolution code=private_state_missing "
            "canonical_write=0"
        )
        return 2

    with tempfile.TemporaryDirectory(prefix="tl-candidate-cycle-") as temp_dir:
        temp = Path(temp_dir)
        index_path = temp / "index.json"
        decisions_path = temp / "semantic-decisions.json"
        index_path.write_bytes(index_raw)
        decisions_path.write_bytes(decisions_raw)
        resolution_cmd = [
            python_exe,
            "src/candidate_resolution.py",
            "--candidate-index",
            str(index_path),
            "--semantic-decisions",
            str(decisions_path),
            "--remote",
            args.remote,
            "--root",
            args.root,
            "--apply",
        ]
        code = run_command(resolution_cmd)
        if code != 0:
            print(
                "candidate_local_cycle_error stage=resolution code=stage_failed "
                "canonical_write=0"
            )
            return code

    code = run_command(finalize_cmd)
    if code != 0:
        print(
            "candidate_local_cycle_error stage=finalize code=stage_failed canonical_write=0"
        )
        return code

    print(
        "candidate_local_cycle_ok "
        f"selected={len(expected_ids)} cycle_version={CYCLE_VERSION} "
        "canonical_write=0 paid_model=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
