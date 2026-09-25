#!/usr/bin/env python3
"""Smoke tests for Stage 14 helper contracts."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_stage_14_verification import (
    STAGE14_VERSION,
    build_failure_report,
    target_index_path,
)


def main() -> None:
    assert STAGE14_VERSION == "0.2.0"
    assert target_index_path("00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-demo.md") == "00_LIBRARY/04_AI_AGENTS/INDEX.md"
    assert target_index_path("00_LIBRARY/SOURCES/source-demo.md") == "00_LIBRARY/SOURCES/INDEX.md"
    assert target_index_path("bad/path.md") is None

    failure = build_failure_report(
        {"batch_id": "b" * 20},
        {"transaction_id": "t" * 20},
        [],
        [],
        [{"id": "semantic-case", "state": "PASS"}],
        {"state": "FAIL", "failed_case_ids": ["semantic-case"]},
        ["semantic_routing_failed:semantic-case"],
    )
    assert failure["state"] == "FAIL"
    assert failure["candidate_settlement_allowed"] is False
    assert failure["canonical_write_performed"] is False
    assert failure["semantic_routing"]["failed_case_ids"] == ["semantic-case"]
    assert failure["errors"] == ["semantic_routing_failed:semantic-case"]
    print("candidate_stage_14_verification_smoke_ok index_reference_rules=1 canonical_write=0")


if __name__ == "__main__":
    main()
