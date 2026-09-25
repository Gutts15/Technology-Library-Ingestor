#!/usr/bin/env python3
"""Build a zero-cost adaptive semantic-validation plan for candidate knowledge.

The planner itself performs no semantic validation and calls no model. It only
looks at private candidate metadata and decides whether pending semantic work is
large/old enough to justify a validation batch when an external scheduler or
manual run asks for a check.

This supports the project policy: check cheaply, do expensive work only when it
is actually due, and never confuse age with an instruction to re-research
canonical knowledge.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
PLAN_VERSION = "0.1.1"
DEFAULT_VOLUME_THRESHOLD = 5
DEFAULT_MAX_WAIT_DAYS = 30
DEFAULT_MAX_BATCH = 20


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def safe_positive(value: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if number > 0 else default


def build_plan(
    candidate_index: dict[str, Any] | None,
    validation_proposals: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    volume_threshold: int = DEFAULT_VOLUME_THRESHOLD,
    max_wait_days: int = DEFAULT_MAX_WAIT_DAYS,
    max_batch: int = DEFAULT_MAX_BATCH,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    volume_threshold = safe_positive(volume_threshold, DEFAULT_VOLUME_THRESHOLD)
    max_wait_days = safe_positive(max_wait_days, DEFAULT_MAX_WAIT_DAYS)
    max_batch = safe_positive(max_batch, DEFAULT_MAX_BATCH)

    if not isinstance(candidate_index, dict) or candidate_index.get("schema_version") != 1:
        return {
            "schema_version": SCHEMA_VERSION,
            "plan_version": PLAN_VERSION,
            "state": "HOLD",
            "should_run": False,
            "reason": "candidate_index_invalid",
            "pending_semantic": 0,
            "selected": 0,
            "selected_candidate_ids": [],
        }
    if not isinstance(validation_proposals, dict) or validation_proposals.get("schema_version") != 1:
        return {
            "schema_version": SCHEMA_VERSION,
            "plan_version": PLAN_VERSION,
            "state": "HOLD",
            "should_run": False,
            "reason": "validation_proposals_invalid",
            "pending_semantic": 0,
            "selected": 0,
            "selected_candidate_ids": [],
        }

    index_entries = candidate_index.get("entries")
    proposal_entries = validation_proposals.get("entries")
    if not isinstance(index_entries, list) or not isinstance(proposal_entries, list):
        return {
            "schema_version": SCHEMA_VERSION,
            "plan_version": PLAN_VERSION,
            "state": "HOLD",
            "should_run": False,
            "reason": "candidate_payload_invalid",
            "pending_semantic": 0,
            "selected": 0,
            "selected_candidate_ids": [],
        }

    metadata: dict[str, dict[str, Any]] = {}
    generated_at = parse_time(candidate_index.get("generated_at"))
    for raw in index_entries:
        if not isinstance(raw, dict) or raw.get("valid") is not True:
            continue
        candidate_id = raw.get("candidate_id")
        if not isinstance(candidate_id, str):
            continue
        # Prefer an actual capture/modification timestamp when available. Older V1
        # indexes do not expose it yet, so LAST_CHECKED is a stable fallback that
        # does not reset every time the queue index is regenerated.
        timestamp = (
            parse_time(raw.get("mod_time"))
            or parse_time(raw.get("captured_at"))
            or parse_time(raw.get("last_checked"))
            or generated_at
        )
        metadata[candidate_id] = {"time": timestamp}

    pending: list[dict[str, Any]] = []
    for raw in proposal_entries:
        if not isinstance(raw, dict) or raw.get("proposal") != "READY_FOR_SEMANTIC":
            continue
        candidate_id = raw.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id not in metadata:
            continue
        pending.append({"candidate_id": candidate_id, "time": metadata[candidate_id]["time"]})

    pending.sort(
        key=lambda item: (
            item["time"] is None,
            item["time"] or now,
            item["candidate_id"],
        )
    )

    oldest_age_days = 0
    if pending and pending[0]["time"] is not None:
        oldest_age_days = max(0, int((now - pending[0]["time"]).total_seconds() // 86400))

    if not pending:
        should_run = False
        state = "IDLE"
        reason = "no_semantic_work"
    elif len(pending) >= volume_threshold:
        should_run = True
        state = "DUE"
        reason = "volume_threshold"
    elif oldest_age_days >= max_wait_days:
        should_run = True
        state = "DUE"
        reason = "max_wait_age"
    else:
        should_run = False
        state = "WAIT"
        reason = "below_thresholds"

    selected_ids = [item["candidate_id"] for item in pending[:max_batch]] if should_run else []

    return {
        "schema_version": SCHEMA_VERSION,
        "plan_version": PLAN_VERSION,
        "state": state,
        "should_run": should_run,
        "reason": reason,
        "policy": {
            "volume_threshold": volume_threshold,
            "max_wait_days": max_wait_days,
            "max_batch": max_batch,
            "periodic_canonical_research": False,
        },
        "pending_semantic": len(pending),
        "oldest_pending_age_days": oldest_age_days,
        "selected": len(selected_ids),
        "selected_candidate_ids": selected_ids,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan adaptive zero-cost candidate semantic validation.")
    parser.add_argument("--candidate-index", type=Path, required=True)
    parser.add_argument("--validation-proposals", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--volume-threshold", type=int, default=DEFAULT_VOLUME_THRESHOLD)
    parser.add_argument("--max-wait-days", type=int, default=DEFAULT_MAX_WAIT_DAYS)
    parser.add_argument("--max-batch", type=int, default=DEFAULT_MAX_BATCH)
    args = parser.parse_args()

    plan = build_plan(
        read_json(args.candidate_index),
        read_json(args.validation_proposals),
        volume_threshold=args.volume_threshold,
        max_wait_days=args.max_wait_days,
        max_batch=args.max_batch,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "candidate_batch_plan_ok "
        f"state={plan['state']} should_run={1 if plan['should_run'] else 0} "
        f"pending={plan['pending_semantic']} selected={plan['selected']} reason={plan['reason']}"
    )
    return 0 if plan["state"] != "HOLD" else 2


if __name__ == "__main__":
    raise SystemExit(main())
