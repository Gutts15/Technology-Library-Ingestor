#!/usr/bin/env python3
"""Smoke test for production restart recovery fixture path isolation."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_production_restart_recovery_fixture import (
    FIXTURE_ROOT,
    MASTER,
    UPDATE_TARGET,
    build_recovery_journal,
    physical_rel,
)


def main() -> None:
    fixture_id = "a" * 24
    tx = "b" * 20
    owner = "candidate-publisher-" + "c" * 12
    journal = build_recovery_journal(
        transaction_id=tx,
        batch_id="d" * 20,
        owner=owner,
    )
    logical_paths = [UPDATE_TARGET, MASTER, journal["journal_path"]]
    logical_paths += [
        entry["snapshot_path"]
        for entry in [*journal["items"], *journal["indexes"]]
        if entry.get("snapshot_path")
    ]
    for logical in logical_paths:
        physical = physical_rel(fixture_id, logical)
        assert physical.startswith(FIXTURE_ROOT + "/" + fixture_id + "/")
        assert not physical.startswith("00_LIBRARY/")
    try:
        physical_rel(fixture_id, "../00_LIBRARY/MASTER_INDEX.md")
        raise AssertionError("unsafe path accepted")
    except ValueError:
        pass
    print(
        "candidate_production_restart_recovery_fixture_smoke_ok "
        "private_scope=1 production_journal_shape=1 physical_canonical_write=0"
    )


if __name__ == "__main__":
    main()
