#!/usr/bin/env python3
"""Smoke tests for deterministic post-mutation index planning and verification."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_canonical_index_rebuild import (
    apply_index_plan,
    build_index_plan,
    verify_index_state,
)

def record(
    rid: str,
    *,
    domain: str,
    category: str,
    title: str,
    slug: str,
) -> tuple[str, bytes]:
    path = f"00_LIBRARY/{domain}/{category}/technology-{slug}.md"
    raw = (
        f"RECORD_ID: {rid}\n"
        "TYPE: TECHNOLOGY\n"
        "STATUS: REFERENCE\n"
        f"DOMAIN: {domain}\n"
        f"CATEGORY: {category}\n"
        f"TITLE: {title}\n"
        "\n# SUMMARY\nFixture.\n"
    ).encode("utf-8")
    return path, raw

def main() -> None:
    p1, r1 = record("a" * 20, domain="04_AI_AGENTS", category="TOOLS", title="Alpha", slug="alpha")
    p2, r2 = record("b" * 20, domain="07_DESIGN_CREATIVE", category="TOOLS", title="Beta", slug="beta")
    store = {p1: r1, p2: r2}

    def read(path: str):
        return store.get(path)

    indexes, records, errors = build_index_plan([p1, p2], read)
    assert not errors, errors
    assert indexes is not None
    assert len(records) == 2
    assert set(indexes) == {
        "00_LIBRARY/MASTER_INDEX.md",
        "00_LIBRARY/04_AI_AGENTS/INDEX.md",
        "00_LIBRARY/07_DESIGN_CREATIVE/INDEX.md",
    }
    assert b"RECORDS: 2" in indexes["00_LIBRARY/MASTER_INDEX.md"]

    def write(path: str, raw: bytes) -> bool:
        store[path] = raw
        return True

    report, errors = apply_index_plan(indexes, read_bytes=read, write_bytes=write)
    assert not errors, errors
    assert report["state"] == "INDEXES_VERIFIED"
    assert report["updated"] == 3
    assert not verify_index_state(indexes, read_bytes=read)

    # Reapply must be idempotent.
    report, errors = apply_index_plan(indexes, read_bytes=read, write_bytes=write)
    assert not errors, errors
    assert report["updated"] == 0
    assert report["unchanged"] == 3

    # A write that lies about success must force recovery.
    broken_store = {p1: r1, p2: r2}
    broken_read = lambda path: broken_store.get(path)
    report, errors = apply_index_plan(
        indexes,
        read_bytes=broken_read,
        write_bytes=lambda path, raw: True,
    )
    assert report["state"] == "RECOVERY_REQUIRED"
    assert errors and errors[0].startswith("index_verify_failed:")

    print(
        "candidate_canonical_index_rebuild_smoke_ok records=2 indexes=3 "
        "idempotent=1 byte_verify=1 failed_write_requires_recovery=1"
    )

if __name__ == "__main__":
    main()
