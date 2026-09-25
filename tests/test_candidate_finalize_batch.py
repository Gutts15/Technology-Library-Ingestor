#!/usr/bin/env python3
"""Smoke tests for deterministic finalizer batch sealing."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_finalize_batch import build_finalize_batch, verify_finalize_batch


def main() -> None:
    master = b"# MASTER_INDEX\nRECORDS: 1\n"
    publish = b'{"schema_version":1,"items":[]}\n'
    validation = b'{"schema_version":1,"items":[],"counts":{"create":0,"unchanged":0}}\n'
    updates = b'{"schema_version":1,"update_ready_version":"0.2.0","canonical_write_performed":false,"items":[]}\n'

    batch, errors = build_finalize_batch(master, publish, validation, updates)
    assert not errors, errors
    assert batch is not None
    assert len(batch["batch_id"]) == 20
    assert batch["canonical_write_performed"] is False
    assert not verify_finalize_batch(batch, master, publish, validation, updates)

    changed_updates = updates + b" "
    changed_errors = verify_finalize_batch(
        batch, master, publish, validation, changed_updates
    )
    assert "finalize_batch_update_manifest_sha256_mismatch" in changed_errors

    changed_master = master + b"\n"
    changed_errors = verify_finalize_batch(
        batch, changed_master, publish, validation, updates
    )
    assert "finalize_batch_master_index_sha256_mismatch" in changed_errors

    empty, empty_errors = build_finalize_batch(master, b"", validation, updates)
    assert empty is None
    assert "publish_manifest_missing" in empty_errors

    print(
        "candidate_finalize_batch_smoke_ok sealed_inputs=1 torn_state_blocked=1 "
        "master_bound=1 canonical_write=0"
    )


if __name__ == "__main__":
    main()
