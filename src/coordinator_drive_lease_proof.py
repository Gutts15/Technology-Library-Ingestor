#!/usr/bin/env python3
"""Marco 2 real-transport proof for the Drive lease coordinator backend.

Runs only against a fresh synthetic private prefix under
99_INBOX/CANDIDATES/COORDINATOR_LEASE_PROOF/<random>/. Never touches
00_LIBRARY, DROP_HERE or production LOTES. Prints status codes only.
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import candidate_production_coordinator as c  # noqa: E402

BASE = "tl:99_INBOX/CANDIDATES/COORDINATOR_LEASE_PROOF"


def check(name: str, ok: bool) -> None:
    print(f"lease_proof {name}={'PASS' if ok else 'FAIL'}")
    if not ok:
        raise SystemExit(1)


def main() -> int:
    url = f"{c.DRIVE_LEASE_PREFIX}{BASE}/{secrets.token_hex(8)}"
    a = dict(transaction_id="tx-a", batch_id="lote-a", owner="runner-a", journal_path="journal/a.json")
    b = dict(transaction_id="tx-b", batch_id="lote-b", owner="runner-b", journal_path="journal/b.json")
    check("initialize", c.ensure_initialized(url)[2] == [])
    sha_free = c.read_state(url)[0]
    check("acquire_exclusive", c.acquire(url, **a)[2] == [])
    check("second_writer_rejected", c.acquire(url, **b)[2] == ["coordinator_busy"])
    check("same_owner_resume_new_process", c.acquire(url, **a)[2] == [])
    work, _, _ = c._drive_prepare(url, c.read_state(url)[0], {"state": "FREE"})
    check("write_boundary", c.mark_canonical_write_started(url, **a) == [])
    check("late_stale_write_fenced", work is not None and not c._push_expected(work, sha_free))
    check("abort_refused_after_write", c.release_prewrite_abort(url, **a) == ["coordinator_release_identity_mismatch"])
    check("release_committed", c.release_committed(url, **a, receipt_sha256="0" * 64) == [])
    _, state, errors = c.read_state(url)
    check("final_free_readback", not errors and state["state"] == "FREE" and state["generation"] == 3)
    print("lease_proof status=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
