#!/usr/bin/env python3
"""Smoke test for private commit-remote fixture isolation."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_transaction_commit_remote_fixture import (
    CONTENT,
    FIXTURE_ROOT,
    MASTER,
    TARGET,
    build_plan,
    fixture_prefix,
    physical_rel,
)


def main() -> None:
    fixture_id = "a" * 24
    prefix = fixture_prefix(fixture_id)
    assert prefix == FIXTURE_ROOT + "/" + fixture_id

    for logical in (TARGET, CONTENT, MASTER):
        physical = physical_rel(fixture_id, logical)
        assert physical.startswith(prefix + "/")
        assert not physical.startswith("00_LIBRARY/")

    plan = build_plan()
    assert plan["counts"]["writes"] == 1
    assert plan["items"][0]["action"] == "CREATE"

    try:
        physical_rel(fixture_id, "../00_LIBRARY/MASTER_INDEX.md")
        raise AssertionError("unsafe path accepted")
    except ValueError:
        pass

    print(
        "candidate_transaction_commit_remote_fixture_smoke_ok "
        "private_scope=1 receipt_journal_scope=1 physical_canonical_write=0"
    )


if __name__ == "__main__":
    main()
