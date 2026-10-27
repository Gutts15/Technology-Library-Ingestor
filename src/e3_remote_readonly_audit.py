#!/usr/bin/env python3
"""E3 read-only audit of the actual private canonical Drive tree.

No Git coordinator transitions, no rclone writes, no model calls, no public
output containing paths, records, evidence, credentials or remote listing.
An optional pending E2 lot is simulated in a fresh ephemeral local mirror.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from candidate_canonical_index_rebuild import build_index_plan
from e1_lote_prepare import LOTES, LOT_RE
from e3_transaction_preflight import PreflightError, simulate
from library_index_build import RECORD_NAME_RE

MAX_FILES = 160
MAX_ITEM = 128 * 1024
MAX_MASTER = 128 * 1024
MAX_LOTE = 200 * 1024
MAX_DECISIONS = 400 * 1024
REMOTE_RE = re.compile(r"^[A-Za-z0-9_-]+:$")
INDEX = "00_LIBRARY/MASTER_INDEX.md"


class AuditFailure(Exception):
    pass


def remote_command(args: list[str], *, timeout: int = 75) -> bytes:
    try:
        process = subprocess.run(
            ["rclone", *args, "--log-level", "ERROR",
             "--stats", "0", "--retries", "1", "--low-level-retries", "1"],
            capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise AuditFailure("storage_unavailable") from None
    if process.returncode:
        raise AuditFailure("storage_unavailable")
    return process.stdout


def read(remote: str, relative: str, limit: int, call=remote_command) -> bytes:
    if (not relative or relative.startswith("/") or "\\" in relative
            or ".." in Path(relative).parts or "//" in relative):
        raise AuditFailure("unsafe_read_path")
    raw = call(["cat", remote + relative, "--count", str(limit + 1)])
    if len(raw) > limit:
        raise AuditFailure("oversize_read")
    return raw


def inventory(remote: str, relative: str, call=remote_command) -> list[str]:
    raw = call(["lsjson", remote + relative, "--recursive", "--files-only"], timeout=120)
    if len(raw) > 1024 * 1024:
        raise AuditFailure("inventory_oversize")
    try:
        items = json.loads(raw)
    except (ValueError, UnicodeError):
        raise AuditFailure("inventory_invalid") from None
    if not isinstance(items, list) or len(items) > 1000:
        raise AuditFailure("inventory_invalid")
    paths: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("Path"), str):
            raise AuditFailure("inventory_invalid")
        value = item["Path"]
        if (not value or value.startswith("/") or "\\" in value
                or ".." in Path(value).parts or "//" in value):
            raise AuditFailure("inventory_invalid")
        paths.append(value)
    if len(set(paths)) != len(paths):
        raise AuditFailure("inventory_duplicate")
    return sorted(paths)


def safe_record_path(path: str) -> bool:
    if (not path.startswith("00_LIBRARY/") or ".." in Path(path).parts
            or "\\" in path or "//" in path):
        return False
    return RECORD_NAME_RE.fullmatch(Path(path).name) is not None


def snapshot_canonical(remote: str, *, read_file: Callable, list_files: Callable
                       ) -> dict[str, bytes]:
    relative_paths = list_files(remote, "00_LIBRARY")
    records = ["00_LIBRARY/" + p for p in relative_paths
               if safe_record_path("00_LIBRARY/" + p)]
    if not 1 <= len(records) <= MAX_FILES:
        raise AuditFailure("canonical_count_invalid")
    baseline = {INDEX: read_file(remote, INDEX, MAX_MASTER)}
    if not baseline[INDEX]:
        raise AuditFailure("master_missing")
    for record in records:
        if record in baseline:
            raise AuditFailure("record_duplicate")
        baseline[record] = read_file(remote, record, MAX_ITEM)
    # Only the canonical generator may decide the required index paths.
    indexes, _, errors = build_index_plan(
        records, lambda p: baseline.get(p))
    if errors or indexes is None:
        raise AuditFailure("canonical_records_invalid")
    if indexes.get(INDEX) != baseline[INDEX]:
        raise AuditFailure("master_stale")
    for path, expected in indexes.items():
        if not (path == INDEX or path.startswith("00_LIBRARY/")):
            raise AuditFailure("index_path_invalid")
        if path != INDEX:
            current = read_file(remote, path, MAX_MASTER)
            if current != expected:
                raise AuditFailure("domain_index_stale")
        baseline[path] = expected
    return baseline


def pending_lot(remote: str, *, read_file: Callable, list_files: Callable
                ) -> tuple[str, bytes, bytes] | None:
    paths = set(list_files(remote, LOTES))
    all_lots = sorted(p for p in paths if LOT_RE.fullmatch(p))
    for rel in all_lots:
        prefix, filename = rel.rsplit("/", 1)
        suffix = filename[4:]
        decisions = prefix + "/decisoes" + suffix
        receipt = prefix + "/recibo" + suffix
        if receipt in paths:
            continue
        if decisions not in paths:
            continue
        lote_raw = read_file(remote, LOTES + "/" + rel, MAX_LOTE)
        decisions_raw = read_file(remote, LOTES + "/" + decisions, MAX_DECISIONS)
        return rel, lote_raw, decisions_raw
    return None


def audit(remote: str, *, inspect_pending: bool = False, read_file=read,
          list_files=inventory) -> dict:
    if not REMOTE_RE.fullmatch(remote):
        raise AuditFailure("remote_invalid")
    baseline = snapshot_canonical(remote, read_file=read_file, list_files=list_files)
    records = sum(1 for path in baseline if safe_record_path(path))
    result = {"status": "CANONICAL_READONLY_VERIFIED",
              "records": records, "pending": "NOT_CHECKED",
              "canonical_write": 0}
    if not inspect_pending:
        return result
    pending = pending_lot(remote, read_file=read_file, list_files=list_files)
    if pending is None:
        return {**result, "pending": "NONE"}
    relative, lote_raw, decisions_raw = pending
    # Never persist snapshots or decisions to a shared Actions artifact.
    with tempfile.TemporaryDirectory(prefix="tl-e3-audit-") as temp:
        root = Path(temp)
        for relative_path, raw in baseline.items():
            dest = root / relative_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
        try:
            preview = simulate(root, lote_raw, relative, decisions_raw)
        except (PreflightError, OSError, ValueError):
            raise AuditFailure("pending_transaction_invalid") from None
    return {**result, "pending": preview["state"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit real Drive read-only; never publish")
    parser.add_argument("--remote", default="tl:")
    parser.add_argument("--inspect-pending", action="store_true")
    args = parser.parse_args()
    try:
        result = audit(args.remote, inspect_pending=args.inspect_pending)
    except (AuditFailure, Exception) as exc:
        # Stable codes only; prevent provider/Drive paths or content in logs.
        code = str(exc) if isinstance(exc, AuditFailure) else "unexpected"
        print("e3_audit status=BLOCKED code=" + code + " canonical_write=0")
        return 2
    print("e3_audit status=" + result["status"] + " records=" +
          str(result["records"]) + " pending=" + result["pending"] +
          " canonical_write=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
