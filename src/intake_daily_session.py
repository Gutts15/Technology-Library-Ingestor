#!/usr/bin/env python3
"""One bounded daily intake session; its snapshot and receipts stay in private Drive."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sys
import time
from typing import Any

from zero_touch_intake import (
    ACCEPTANCE_FIXTURE, MAX_ITEM_BYTES, RcloneStore, IntakeError,
    identity, process, revision, source_name,
)

MAX_SESSION_RUNS = 24
MAX_SESSION_SECONDS = 8 * 60 * 60


def _target(item: dict[str, Any], fixture_only: bool) -> tuple[str, str] | None:
    if fixture_only and not ACCEPTANCE_FIXTURE.fullmatch(source_name(item) or ""):
        return None
    key, rev, name, size = identity(item), revision(item), source_name(item), item.get("Size")
    if not key or not rev or not name or not isinstance(size, int) or not 0 <= size <= MAX_ITEM_BYTES:
        return None
    return key, rev


def _snapshot(store: RcloneStore, items: list[dict[str, Any]], fixture_only: bool) -> list[list[str]]:
    targets: set[tuple[str, str]] = set()
    for item in items:
        target = _target(item, fixture_only)
        if target and target[1] not in store.read_state(target[0])["completed"]:
            targets.add(target)
    return [list(target) for target in sorted(targets)]


def run_session(
    store: RcloneStore, *, continuation: bool, fixture_only: bool,
    now: float | None = None, cycle: str | None = None,
) -> dict[str, Any]:
    wall_now = time.time() if now is None else now
    cycle = cycle or dt.datetime.fromtimestamp(wall_now, dt.timezone.utc).date().isoformat()
    if not store.baseline_complete():
        raise IntakeError("baseline_required")
    session = store.read_session()
    if continuation:
        if session is None or session.get("status") != "open":
            return {"continue": False, "reason": "closed"}
    else:
        if session is not None and session.get("cycle") == cycle:
            return {"continue": False, "reason": "already_started"}
        items = store.list_inbox()
        session = {
            "cycle": cycle, "status": "open", "started_at": wall_now,
            "runs": 0, "targets": _snapshot(store, items, fixture_only),
            "fixture_only": fixture_only,
        }
        store.write_session(session)
    if not isinstance(session.get("targets"), list) or session.get("fixture_only") != fixture_only:
        raise IntakeError("invalid_private_session")
    if session["runs"] >= MAX_SESSION_RUNS or wall_now - session["started_at"] >= MAX_SESSION_SECONDS:
        session.update(status="closed", reason="ceiling")
        store.write_session(session)
        return {"continue": False, "reason": "ceiling"}
    if not session["targets"]:
        session.update(status="closed", reason="empty")
        store.write_session(session)
        return {"continue": False, "reason": "empty"}
    session["runs"] += 1
    store.write_session(session)
    eligible = {tuple(target) for target in session["targets"]}
    completed: set[tuple[str, str]] = set()
    present: set[tuple[str, str]] = set()
    stats = process(
        store, fixture_only=fixture_only, now=wall_now, eligible=eligible,
        completed_out=completed, present_out=present,
    )
    remaining = eligible - completed
    # Removed or revised objects are not part of this frozen session.
    remaining &= present
    session["targets"] = [list(target) for target in sorted(remaining)]
    elapsed = time.time() - wall_now if now is None else 0
    if not remaining:
        session.update(status="closed", reason="drained")
    elif session["runs"] >= MAX_SESSION_RUNS or wall_now + elapsed - session["started_at"] >= MAX_SESSION_SECONDS:
        session.update(status="closed", reason="ceiling")
    elif stats["enqueued"] == 0:
        # A lease or byte-limit stall must not generate a dispatch loop.
        session.update(status="closed", reason="stalled")
    store.write_session(session)
    return {"continue": session["status"] == "open", "reason": session.get("reason", "backlog")}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one private daily-session worker")
    parser.add_argument("--remote", default="tl:")
    parser.add_argument("--continue", dest="continuation", action="store_true")
    parser.add_argument("--fixture-only", action="store_true")
    parser.add_argument("--github-output")
    args = parser.parse_args()
    if not shutil.which("rclone"):
        print("session_error code=tool_missing")
        return 2
    try:
        result = run_session(
            RcloneStore(args.remote), continuation=args.continuation,
            fixture_only=args.fixture_only,
        )
        if args.github_output:
            with open(args.github_output, "a", encoding="utf-8") as handle:
                handle.write(f"continue={str(result['continue']).lower()}\n")
    except IntakeError as exc:
        print(f"session_error code={exc}")
        return 2
    print(f"session_ok continuation={str(result['continue']).lower()} reason={result['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
