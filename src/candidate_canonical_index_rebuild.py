#!/usr/bin/env python3
"""Deterministic post-mutation canonical index planner and verifier.

This module contains no CLI and no remote storage code. It turns an explicit set of
canonical raw-record paths plus byte readers into the exact generated index bytes
expected after a candidate transaction, then can write/verify only those generated
index paths through caller-supplied callbacks.

It deliberately does not authorize publication, acquire coordination, or perform
record mutations.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from library_index_build import planned_indexes, validate_record

INDEX_ENGINE_VERSION = "0.1.0"

ReadBytes = Callable[[str], bytes | None]
WriteBytes = Callable[[str, bytes], bool]

def load_records_from_paths(
    record_paths: Iterable[str],
    read_bytes: ReadBytes,
) -> tuple[list[dict[str, str]], list[str]]:
    records: list[dict[str, str]] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    seen_titles: set[tuple[str, str]] = set()

    for path in sorted(set(record_paths)):
        if not isinstance(path, str) or not path.startswith("00_LIBRARY/") or not path.endswith(".md"):
            errors.append(f"record_path_invalid:{path}")
            continue
        raw = read_bytes(path)
        if raw is None:
            errors.append(f"record_missing:{path}")
            continue
        try:
            record = validate_record(path, raw)
        except ValueError as exc:
            errors.append(f"{exc}:{path}")
            continue

        rid = record["record_id"]
        title_key = (record["record_type"], record["title"].casefold())
        if rid in seen_ids:
            errors.append(f"duplicate_record_id:{rid}")
        if title_key in seen_titles:
            errors.append(f"duplicate_canonical_title:{record['record_type']}:{record['title']}")
        seen_ids.add(rid)
        seen_titles.add(title_key)
        records.append(record)

    if errors:
        return [], sorted(set(errors))
    records.sort(key=lambda r: (r["domain"], r["category"], r["record_type"], r["title"].casefold()))
    return records, []

def build_index_plan(
    record_paths: Iterable[str],
    read_bytes: ReadBytes,
) -> tuple[dict[str, bytes] | None, list[dict[str, str]], list[str]]:
    records, errors = load_records_from_paths(record_paths, read_bytes)
    if errors:
        return None, [], errors
    if not records:
        return None, [], ["no_canonical_records"]
    return planned_indexes(records), records, []

def apply_index_plan(
    indexes: dict[str, bytes],
    *,
    read_bytes: ReadBytes,
    write_bytes: WriteBytes,
) -> tuple[dict[str, Any], list[str]]:
    updated = 0
    unchanged = 0
    errors: list[str] = []

    for path, expected in sorted(indexes.items()):
        if not (
            path == "00_LIBRARY/MASTER_INDEX.md"
            or (path.startswith("00_LIBRARY/") and path.endswith("/INDEX.md"))
        ):
            errors.append(f"unsafe_index_target:{path}")
            continue
        current = read_bytes(path)
        if current == expected:
            unchanged += 1
            continue
        if not write_bytes(path, expected):
            errors.append(f"index_write_failed:{path}")
            break
        updated += 1
        if read_bytes(path) != expected:
            errors.append(f"index_verify_failed:{path}")
            break

    state = "INDEXES_VERIFIED" if not errors else "RECOVERY_REQUIRED"
    return {
        "index_engine_version": INDEX_ENGINE_VERSION,
        "state": state,
        "indexes": len(indexes),
        "updated": updated,
        "unchanged": unchanged,
    }, sorted(set(errors))

def verify_index_state(
    indexes: dict[str, bytes],
    *,
    read_bytes: ReadBytes,
) -> list[str]:
    errors: list[str] = []
    for path, expected in sorted(indexes.items()):
        actual = read_bytes(path)
        if actual != expected:
            errors.append(f"index_bytes_mismatch:{path}")
    return sorted(set(errors))
