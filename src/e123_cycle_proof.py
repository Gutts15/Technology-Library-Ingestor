#!/usr/bin/env python3
"""Marco 3 proof: real Drive + real Gemini, synthetic prefix only.

Seeds a unique private prefix with a tiny synthetic library and two synthetic
DROP_HERE files, runs two full cycles in fresh mirrors (as separate runners
would), checks settlement, idempotency, lease state and that sources were
never touched, then purges ONLY that prefix. Prints counts only.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e123_cloud_cycle as cycle  # noqa: E402
from candidate_production_coordinator import read_state  # noqa: E402
from library_index_build import planned_indexes, validate_record  # noqa: E402

PROOF_ROOT = "99_INBOX/CANDIDATES/E123_CYCLE_PROOF"
PRIOR = "00_LIBRARY/02_WEB_DEVELOPMENT/UI_COMPONENTS/technology-prior-tool.md"
SOURCES = {
    "proof-tool.txt": "Pixel Rig Tool is an open source library that enables skeletal rigging "
                      "workflows for 2D sprites in web games, with a JavaScript API.\n",
    "proof-noise.txt": "Grocery list: buy milk, eggs and bread tomorrow morning please.\n",
}


def main() -> int:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("e123_proof status=SECRET_MISSING")
        return 2
    prefix = PROOF_ROOT + "/" + secrets.token_hex(12)
    remote = cycle.RcloneRemote("tl:" + prefix + "/")
    coordinator = "drive-lease:tl:" + prefix + "/" + cycle.LEASE_DIR
    ok = False
    try:
        baseline = ("RECORD_ID: 0123456789abcdef0123\nTYPE: TECHNOLOGY\nSTATUS: REFERENCE\n"
                    "DOMAIN: 02_WEB_DEVELOPMENT\nCATEGORY: UI_COMPONENTS\nTITLE: Prior Tool\n"
                    "# Prior Tool\n\n## SUMMARY\nPrior Tool retains existing knowledge.\n").encode()
        seed = {PRIOR: baseline, **planned_indexes([validate_record(PRIOR, baseline)])}
        seed.update({cycle.DROP + "/" + n: t.encode() for n, t in SOURCES.items()})
        for rel, raw in seed.items():
            if not remote.write(rel, raw) or remote.read(rel) != raw:
                raise cycle.CycleError("seed_failed")
        with tempfile.TemporaryDirectory(prefix="tl-proof-") as temp:
            r1 = cycle.run_cycle(Path(temp) / "m1", remote, coordinator, api_key=key)
            r2 = cycle.run_cycle(Path(temp) / "m2", remote, coordinator, api_key=key)
        settled = sum(r1["e3"].values())
        actions = {k: v for k, v in r1.items() if k.startswith("action_")}
        print("e123_proof cycle1 extracted=%d held=%d decisions=%d settled=%d writes=%d actions=%s"
              % (r1["e1_extracted"], r1["e1_held"], r1["e2_decisions"], settled,
                 r1["remote_writes"], json.dumps(actions, sort_keys=True)))
        print("e123_proof cycle2 extracted=%d decisions=%d settled=%d drop_new=%d"
              % (r2["e1_extracted"], r2["e2_decisions"], sum(r2["e3"].values()), r2["drop_new"]))
        _, state, errors = read_state(coordinator)
        checks = {
            "cycle1_extracted_2": r1["e1_extracted"] == 2,
            "cycle1_decided": r1["e2_written"] == 1 and r1["e2_decisions"] >= 1,
            "cycle1_settled": set(r1["e3"]) <= {"COMMITTED_RELEASED", "NO_WRITE_SETTLED"} and settled == 1,
            "cycle2_idempotent": (r2["e1_extracted"], r2["e2_decisions"], r2["e3"], r2["drop_new"]) == (0, 0, {}, 0),
            "lease_free": not errors and isinstance(state, dict) and state.get("state") == "FREE",
            "sources_untouched": all(remote.read(cycle.DROP + "/" + n) == t.encode() for n, t in SOURCES.items()),
            "markers_local_only": remote.read(cycle.e1_source_prepare.MARKER) is False,
        }
        print("e123_proof checks " + " ".join(f"{k}={int(v)}" for k, v in checks.items()))
        ok = all(checks.values())
    except cycle.UncertainRemote:
        print("e123_proof status=UNCERTAIN_STOPPED")
        return cycle.EXIT_UNCERTAIN
    except Exception as exc:
        print("e123_proof status=BLOCKED code=" + (str(exc) if type(exc).__name__ in {"CycleError", "DecisionError", "SourcePreparationError",
                                              "CommitFixtureError", "PreparationError"} else type(exc).__name__))
        return 2
    if ok:
        purge = remote._run(["purge", "tl:" + prefix], timeout=300)
        print("e123_proof cleanup=" + ("1" if purge.returncode == 0 else "0"))
        print("e123_proof status=PASS canonical_write_real_library=0")
        return 0
    print("e123_proof status=FAIL prefix_kept_for_inspection=1")
    return 2


if __name__ == "__main__":
    shutil.which("rclone") or sys.exit("rclone missing")
    raise SystemExit(main())
