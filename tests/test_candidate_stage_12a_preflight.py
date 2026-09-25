#!/usr/bin/env python3
"""Smoke test for Stage 12A report validation helpers."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_stage_12a_preflight import validate_stage11g


def main() -> None:
    good = {
        "state": "PASS",
        "stage": "11G",
        "local_recovery_engine": True,
        "remote_restart_fixture": True,
        "real_batch_plan": True,
        "production_coordinator_free": True,
        "canonical_apply_disabled": True,
        "failures": [],
        "canonical_write_performed": False,
    }
    assert validate_stage11g(good) == []

    bad = dict(good)
    bad["canonical_apply_disabled"] = False
    assert "stage11g_missing:canonical_apply_disabled" in validate_stage11g(bad)

    print(
        "candidate_stage_12a_preflight_smoke_ok "
        "stage11_bound=1 canonical_apply_disabled_required=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
