#!/usr/bin/env python3
"""Basic import smoke for Stage 13 verifier."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_stage_13_verification import STAGE13_VERSION, strict_record_paths


def main() -> None:
    assert STAGE13_VERSION == "0.1.0"
    assert callable(strict_record_paths)
    print("candidate_stage_13_verification_smoke_ok read_only=1 canonical_write=0")


if __name__ == "__main__":
    main()
