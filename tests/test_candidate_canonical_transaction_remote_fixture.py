#!/usr/bin/env python3
"""Smoke test for integrated remote transaction fixture path isolation."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_canonical_transaction_remote_fixture import (
    AI_INDEX,
    DESIGN_INDEX,
    FIXTURE_ROOT,
    MASTER,
    build_plan,
    initial_indexes,
    physical_rel,
)


def main() -> None:
    fixture_id = "a" * 24
    plan = build_plan()

    for item in plan["items"]:
        for key in ("target_path", "content_path"):
            logical = item.get(key)
            if not isinstance(logical, str):
                continue
            physical = physical_rel(fixture_id, logical)
            assert physical.startswith(FIXTURE_ROOT + "/" + fixture_id + "/")
            assert not physical.startswith("00_LIBRARY/")

    for logical in (MASTER, AI_INDEX, DESIGN_INDEX):
        physical = physical_rel(fixture_id, logical)
        assert physical.startswith(FIXTURE_ROOT + "/" + fixture_id + "/")
        assert not physical.startswith("00_LIBRARY/")

    indexes = initial_indexes()
    assert set(indexes) == {MASTER, AI_INDEX}
    assert b"RECORDS: 2" in indexes[MASTER]

    try:
        physical_rel(fixture_id, "../00_LIBRARY/MASTER_INDEX.md")
        raise AssertionError("unsafe path accepted")
    except ValueError:
        pass

    print(
        "candidate_canonical_transaction_remote_fixture_smoke_ok "
        "private_scope=1 record_paths=1 index_paths=1 physical_canonical_write=0"
    )


if __name__ == "__main__":
    main()
