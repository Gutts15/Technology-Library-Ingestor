#!/usr/bin/env python3
"""Bounded, private, resumable polling of the single DROP_HERE folder.

Only opaque digests and revision receipts are stored in the private Drive.
GitHub Actions serializes this poller with the existing ingest workflow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import PurePosixPath
from typing import Any

from media_router import TARGETS, classify_item
from rclone_paths import join_remote

INBOX = "99_INBOX/DROP_HERE"
LEDGER = "99_INBOX/PROCESS_LOG/ZERO_TOUCH_INTAKE"
SESSION_STATE = f"{LEDGER}/daily_session.json"
MAX_ITEMS = 5
MAX_BYTES = 512 * 1024 * 1024
MAX_ITEM_BYTES = 512 * 1024 * 1024
MAX_SECONDS = 15 * 60
LEASE_SECONDS = 20 * 60
ACCEPTANCE_FIXTURE = re.compile(r"^STAGE20_SYNTHETIC_20260925_[A-Z]{1,3}\.txt$")


class IntakeError(Exception):
    pass


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def identity(item: dict[str, Any]) -> str | None:
    provider_id = item.get("ID")
    return digest(provider_id) if isinstance(provider_id, str) and provider_id else None


def revision(item: dict[str, Any]) -> str | None:
    size = item.get("Size")
    modtime = item.get("ModTime")
    if not isinstance(size, int) or not isinstance(modtime, str) or not modtime:
        return None
    hashes = item.get("Hashes") if isinstance(item.get("Hashes"), dict) else {}
    basis = json.dumps([size, modtime, hashes], sort_keys=True, separators=(",", ":"))
    return digest(basis)


def safe_suffix(item: dict[str, Any]) -> str:
    name = item.get("Name") or item.get("Path") or ""
    suffix = PurePosixPath(str(name)).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ".bin"


def copy_matches(source: dict[str, Any], destination: dict[str, Any] | None) -> bool:
    if destination is None or destination.get("Size") != source.get("Size"):
        return False
    source_hashes = source.get("Hashes") or {}
    destination_hashes = destination.get("Hashes") or {}
    for algorithm, value in source_hashes.items():
        if value and destination_hashes.get(algorithm) and destination_hashes[algorithm] != value:
            return False
    return True


def source_name(item: dict[str, Any]) -> str | None:
    path = item.get("Path") or item.get("Name")
    if not isinstance(path, str) or not path or path.startswith("/"):
        return None
    parts = PurePosixPath(path).parts
    return path if len(parts) == 1 and parts[0] not in (".", "..") else None


class RcloneStore:
    def __init__(self, remote: str):
        self.remote = remote

    def call(self, *args: str) -> bytes:
        result = subprocess.run(
            ["rclone", *args, "--log-level", "ERROR"], capture_output=True, check=False
        )
        if result.returncode:
            # rclone errors may contain private names and provider IDs.
            raise IntakeError("storage_operation_failed")
        return result.stdout

    def path(self, relative: str) -> str:
        return join_remote(self.remote, relative)

    def list_inbox(self) -> list[dict[str, Any]]:
        raw = self.call("lsjson", self.path(INBOX), "--files-only", "--hash")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise IntakeError("invalid_inbox_listing") from exc
        if not isinstance(value, list):
            raise IntakeError("invalid_inbox_listing")
        return [item for item in value if isinstance(item, dict)]

    def stat(self, relative: str) -> dict[str, Any] | None:
        result = subprocess.run(
            ["rclone", "lsjson", self.path(relative), "--stat", "--hash", "--log-level", "ERROR"],
            capture_output=True, check=False,
        )
        if result.returncode:
            # A missing object is expected. Other errors must not silently turn
            # into absence; verify the parent directory remains accessible.
            parent = str(PurePosixPath(relative).parent)
            self.call("lsjson", self.path(parent), "--dirs-only")
            return None
        try:
            value = json.loads(result.stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            raise IntakeError("invalid_object_metadata") from exc
        if not isinstance(value, dict):
            raise IntakeError("invalid_object_metadata")
        return value

    def read_state(self, key: str) -> dict[str, Any]:
        self.call("mkdir", self.path(LEDGER))
        relative = f"{LEDGER}/{key}.json"
        if self.stat(relative) is None:
            return {"completed": [], "lease": None}
        raw = self.call("cat", self.path(relative))
        try:
            state = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise IntakeError("invalid_private_state") from exc
        if not isinstance(state, dict) or not isinstance(state.get("completed"), list):
            raise IntakeError("invalid_private_state")
        return state

    def write_state(self, key: str, state: dict[str, Any]) -> None:
        self.call("mkdir", self.path(LEDGER))
        with tempfile.TemporaryDirectory() as directory:
            local = os.path.join(directory, "state.json")
            with open(local, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            self.call("copyto", local, self.path(f"{LEDGER}/{key}.json"))

    def enqueue(self, source: str, destination: str) -> None:
        self.call("copyto", self.path(source), self.path(destination), "--no-traverse")

    def ensure_destination_dir(self, destination: str) -> None:
        self.call("mkdir", self.path(str(PurePosixPath(destination).parent)))

    def baseline_complete(self) -> bool:
        self.call("mkdir", self.path(LEDGER))
        return self.stat(f"{LEDGER}/baseline_complete.json") is not None

    def mark_baseline_complete(self) -> None:
        self.write_state("baseline_complete", {"completed": [], "lease": None})

    def read_session(self) -> dict[str, Any] | None:
        self.call("mkdir", self.path(LEDGER))
        if self.stat(SESSION_STATE) is None:
            return None
        try:
            value = json.loads(self.call("cat", self.path(SESSION_STATE)))
        except (ValueError, UnicodeDecodeError) as exc:
            raise IntakeError("invalid_private_session") from exc
        if not isinstance(value, dict) or not isinstance(value.get("targets"), list):
            raise IntakeError("invalid_private_session")
        return value

    def write_session(self, value: dict[str, Any]) -> None:
        self.call("mkdir", self.path(LEDGER))
        with tempfile.TemporaryDirectory() as directory:
            local = os.path.join(directory, "session.json")
            with open(local, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            self.call("copyto", local, self.path(SESSION_STATE))


def process(
    store: RcloneStore, *, baseline: bool = False, fixture_only: bool = False,
    now: float | None = None, eligible: set[tuple[str, str]] | None = None,
    completed_out: set[tuple[str, str]] | None = None,
    present_out: set[tuple[str, str]] | None = None,
    items: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    started = time.monotonic()
    wall_now = time.time() if now is None else now
    stats = {name: 0 for name in ("scanned", "enqueued", "already_done", "baselined", "leased", "deferred", "unsupported", "excluded")}
    items = store.list_inbox() if items is None else items
    stats["scanned"] = len(items)
    if present_out is not None:
        present_out.update(
            (key, rev) for item in items
            if (key := identity(item)) and (rev := revision(item))
        )
    # Deterministic ordering keeps a large backlog from starving old items.
    items.sort(key=lambda item: (str(item.get("ModTime") or ""), identity(item) or ""))
    total_bytes = 0
    for item in items:
        if time.monotonic() - started >= MAX_SECONDS:
            stats["deferred"] += 1
            break
        if fixture_only and not ACCEPTANCE_FIXTURE.fullmatch(source_name(item) or ""):
            stats["excluded"] += 1
            continue
        key, rev, name = identity(item), revision(item), source_name(item)
        if eligible is not None and (key, rev) not in eligible:
            continue
        size = item.get("Size")
        if not key or not rev or not name or not isinstance(size, int) or size < 0:
            stats["unsupported"] += 1
            continue
        state = store.read_state(key)
        if rev in state["completed"]:
            stats["already_done"] += 1
            if completed_out is not None:
                completed_out.add((key, rev))
            continue
        if baseline:
            state["completed"].append(rev)
            state["lease"] = None
            store.write_state(key, state)
            stats["baselined"] += 1
            continue
        lease = state.get("lease")
        if isinstance(lease, dict) and lease.get("revision") == rev and lease.get("until", 0) > wall_now:
            stats["leased"] += 1
            continue
        if size > MAX_ITEM_BYTES or total_bytes + size > MAX_BYTES or stats["enqueued"] >= MAX_ITEMS:
            stats["deferred"] += 1
            continue
        kind = classify_item(item)
        dest_name = f"source_{key[:20]}_{rev[:12]}{safe_suffix(item)}"
        destination = f"{TARGETS[kind]}/{dest_name}"
        state["lease"] = {"revision": rev, "until": wall_now + LEASE_SECONDS}
        store.write_state(key, state)
        try:
            current_source = store.stat(f"{INBOX}/{name}")
            if current_source is None or identity(current_source) != key or revision(current_source) != rev:
                raise IntakeError("source_changed_before_enqueue")
            store.ensure_destination_dir(destination)
            remote_stat = store.stat(destination)
            if remote_stat is None:
                store.enqueue(f"{INBOX}/{name}", destination)
                remote_stat = store.stat(destination)
            if not copy_matches(item, remote_stat):
                raise IntakeError("enqueue_verification_failed")
            current_source = store.stat(f"{INBOX}/{name}")
            if current_source is None or identity(current_source) != key or revision(current_source) != rev:
                raise IntakeError("source_changed_during_enqueue")
            state["completed"].append(rev)
            state["lease"] = None
            store.write_state(key, state)
        except IntakeError:
            # An expired lease resumes deterministically at the same destination.
            raise
        stats["enqueued"] += 1
        if completed_out is not None:
            completed_out.add((key, rev))
        total_bytes += size
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Poll private Drive without logging source metadata")
    parser.add_argument("--remote", default="tl:")
    parser.add_argument("--baseline-existing", action="store_true")
    parser.add_argument("--fixture-only", action="store_true")
    args = parser.parse_args()
    if not shutil.which("rclone"):
        print("intake_error code=tool_missing")
        return 2
    try:
        store = RcloneStore(args.remote)
        if args.baseline_existing:
            if store.baseline_complete():
                raise IntakeError("baseline_already_complete")
            stats = process(store, baseline=True)
            if stats["unsupported"] or stats["deferred"]:
                raise IntakeError("baseline_incomplete")
            store.mark_baseline_complete()
        else:
            if not store.baseline_complete():
                raise IntakeError("baseline_required")
            stats = process(store, fixture_only=args.fixture_only)
    except IntakeError as exc:
        print(f"intake_error code={exc}")
        return 2
    print("intake_ok " + " ".join(f"{key}={value}" for key, value in stats.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
