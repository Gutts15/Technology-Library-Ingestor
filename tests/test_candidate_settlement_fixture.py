#!/usr/bin/env python3
"""Smoke tests for idempotent fixture candidate settlement."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_settlement_fixture import apply_settlement
from candidate_settlement_plan import SETTLEMENT_PLAN_VERSION
from candidate_transaction_fixture import FIXTURE_MARKER, FIXTURE_MARKER_VALUE


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def make_plan(a: bytes, b: bytes) -> dict:
    txid = "a" * 20
    return {
        "schema_version": 1,
        "settlement_plan_version": SETTLEMENT_PLAN_VERSION,
        "transaction_id": txid,
        "receipt_version": "0.2.0",
        "canonical_publication_verified": True,
        "private_lifecycle_write_performed": False,
        "canonical_write_performed": False,
        "items": [
            {
                "candidate_id": sha(a)[:20],
                "candidate_sha256": sha(a),
                "decision": "VALIDATED_NEW",
                "lane": "NEW",
                "action": "CREATE",
                "record_id": "b" * 20,
                "target_path": "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-a.md",
                "source_path": "READY_FOR_CURATION/candidate-a.md",
                "destination_path": f"RESOLVED/PUBLISHED/{txid}/candidate-a.md",
            },
            {
                "candidate_id": sha(b)[:20],
                "candidate_sha256": sha(b),
                "decision": "VALIDATED_UPDATE",
                "lane": "UPDATE",
                "action": "UPDATE",
                "record_id": "c" * 20,
                "target_path": "00_LIBRARY/04_AI_AGENTS/INTEGRATIONS/technology-b.md",
                "source_path": "READY_FOR_CURATION/candidate-b.md",
                "destination_path": f"RESOLVED/PUBLISHED/{txid}/candidate-b.md",
            },
        ],
        "counts": {"candidates": 2, "create": 1, "update": 1, "unchanged": 0},
    }


def prepare(root: Path, a: bytes, b: bytes) -> None:
    (root / FIXTURE_MARKER).write_text(FIXTURE_MARKER_VALUE, encoding="utf-8")
    ready = root / "99_INBOX/CANDIDATES/READY_FOR_CURATION"
    ready.mkdir(parents=True, exist_ok=True)
    (ready / "candidate-a.md").write_bytes(a)
    (ready / "candidate-b.md").write_bytes(b)


def main() -> None:
    a = b"candidate a\n"
    b = b"candidate b\n"
    plan = make_plan(a, b)
    txid = plan["transaction_id"]

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        prepare(root, a, b)
        report, errors = apply_settlement(root, plan)
        assert not errors, errors
        assert report is not None
        assert report["state"] == "SETTLED_FIXTURE"
        assert report["candidates"] == 2
        assert report["moves"] == 2
        assert report["canonical_write_performed"] is False
        published = root / f"99_INBOX/CANDIDATES/RESOLVED/PUBLISHED/{txid}"
        assert (published / "candidate-a.md").read_bytes() == a
        assert (published / "candidate-b.md").read_bytes() == b
        assert not (root / "99_INBOX/CANDIDATES/READY_FOR_CURATION/candidate-a.md").exists()
        state = root / f"99_INBOX/CANDIDATES/SETTLEMENTS/{txid}.json"
        assert state.exists()

    # A partial move is recoverable by rerunning the same verified plan.
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        prepare(root, a, b)
        partial, partial_errors = apply_settlement(root, plan, fail_after_moves=1)
        assert partial_errors == ["injected_failure_after_move"]
        assert partial is not None and partial["state"] == "PARTIAL_RECOVERABLE"
        assert partial["moves_before_failure"] == 1

        recovered, recovered_errors = apply_settlement(root, plan)
        assert not recovered_errors, recovered_errors
        assert recovered is not None
        assert recovered["state"] == "SETTLED_FIXTURE"
        assert recovered["recovered_existing_destinations"] == 1
        published = root / f"99_INBOX/CANDIDATES/RESOLVED/PUBLISHED/{txid}"
        assert (published / "candidate-a.md").read_bytes() == a
        assert (published / "candidate-b.md").read_bytes() == b

    # The explicit fixture marker remains mandatory.
    with tempfile.TemporaryDirectory() as temp:
        report, errors = apply_settlement(Path(temp), plan)
        assert report is None
        assert errors == ["fixture_marker_missing"]

    print(
        "candidate_settlement_fixture_smoke_ok settle=1 partial_recovery=1 "
        "idempotent=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
