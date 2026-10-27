#!/usr/bin/env python3
"""One-shot real GitHub coordinator acceptance, no Drive/canonical writes.

Tests the repository's actual shared Git exact-ref coordinator under the
existing GitHub Actions token. Leaves FREE after a verified prewrite abort.
Never marks canonical_write_performed or creates a publication journal.
"""
from __future__ import annotations

import os
import sys
import uuid

from candidate_production_coordinator import (
    acquire, ensure_initialized, read_state, release_prewrite_abort,
)


class ProbeError(Exception):
    pass


def probe(remote: str) -> dict:
    _, existing, errors = ensure_initialized(remote)
    if errors or not isinstance(existing, dict):
        raise ProbeError("INITIALIZE_UNCONFIRMED")
    if existing.get("state") != "FREE":
        raise ProbeError("COORDINATOR_NOT_FREE")
    generation = existing.get("generation")
    if not isinstance(generation, int):
        raise ProbeError("GENERATION_INVALID")
    tx = uuid.uuid4().hex[:20]
    batch = "e3-smoke-" + tx
    owner = "candidate-publisher-" + uuid.uuid4().hex[:12]
    journal = ("99_INBOX/CANDIDATES/PRODUCTION_RECOVERY/" + tx + "/" +
               owner + "/journal.json")
    params = dict(transaction_id=tx, batch_id=batch, owner=owner, journal_path=journal)
    _, active, problem = acquire(remote, **params)
    if problem or not isinstance(active, dict):
        # A lost ACK may have left this exact owner active. Fail closed;
        # no automatic retry, reset, force-push or unrelated owner takeover.
        raise ProbeError("ACQUIRE_UNCERTAIN")
    _, observed, problem = read_state(remote)
    if (problem or observed is None or observed.get("state") != "ACTIVE"
            or observed.get("canonical_write_performed") is not False
            or any(observed.get(k) != v for k, v in params.items())
            or observed.get("generation", 0) <= generation):
        raise ProbeError("ACTIVE_OWNER_UNVERIFIED")
    other = dict(params, transaction_id="f" * 20,
                 owner="candidate-publisher-" + "f" * 12)
    _, _, denied = acquire(remote, **other)
    if "coordinator_busy" not in denied:
        raise ProbeError("COMPETITOR_NOT_BLOCKED")
    if release_prewrite_abort(remote, **params):
        raise ProbeError("PREWRITE_RELEASE_UNCERTAIN")
    _, final, problem = read_state(remote)
    if (problem or not isinstance(final, dict) or final.get("state") != "FREE"
            or final.get("release_reason") != "PREWRITE_ABORT_VERIFIED"
            or final.get("released_transaction_id") != tx
            or final.get("generation", 0) <= observed["generation"]
            or final.get("canonical_write_performed") is not False):
        raise ProbeError("FREE_READBACK_INVALID")
    return {"status": "PASS", "lease": 1, "competitor_denied": 1,
            "release_verified": 1, "canonical_write": 0}


def main() -> int:
    expected = "Gutts15/Technology-Library-Ingestor"
    if (os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_REPOSITORY") != expected):
        print("e3_github_coord status=BLOCKED code=WRONG_REPOSITORY canonical_write=0")
        return 2
    token = os.environ.get("GH_COORDINATOR_TOKEN", "")
    if not token:
        print("e3_github_coord status=BLOCKED code=TOKEN_MISSING canonical_write=0")
        return 2
    remote = "https://x-access-token:" + token + "@github.com/" + expected + ".git"
    try:
        result = probe(remote)
    except ProbeError as exc:
        # Never log Git URL, credentials or identity hashes.
        print("e3_github_coord status=BLOCKED code=" + str(exc) + " canonical_write=0")
        return 2
    except Exception:
        print("e3_github_coord status=BLOCKED code=UNEXPECTED canonical_write=0")
        return 2
    print("e3_github_coord status=PASS lease=1 competitor_denied=1 "
          "release_verified=1 canonical_write=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
