# Marco 3 — E1 → E2 → E3 cloud cycle

`src/e123_cloud_cycle.py` runs one complete cycle in GitHub Actions, reusing the
existing engines with no change to their decision rules:

1. **Mirror**: bounded `rclone copy` of `00_LIBRARY`, `LOTES`, E1 receipts,
   `FILE_EVIDENCE`, `PRODUCTION_RECOVERY` and the `evidence-summary.json` files
   into `$RUNNER_TEMP`. That location is outside the git checkout, which E1 requires.
2. **Recovery first**: if the Drive lease is ACTIVE:
   - before any canonical write, the pre-write lease is released once;
   - after a committed write, the terminal receipt is settled and the lease is released;
   - after a write that started but did not commit, the cycle stops (`recovery_required`).
3. **E1**: lists `DROP_HERE` with Drive SHA-256 hashes and skips sources that already
   have a receipt. It then downloads at most 20 new files, read-only, to a temp
   directory, and runs `prepare_sources`. Sources are never moved or deleted.
4. **E2**: `decide(..., apply=True)` with the real Gemini free tier (`GEMINI_API_KEY`).
5. **E3**: `exercise_commit` for every lot that has decisions but no terminal receipt,
   using the Drive lease (`drive-lease:tl:99_INBOX/CURATION/COORDINATOR`).

**Writes**
- Every durable write made by the engines is mirrored to Drive with a byte readback.
  Immutable files are never overwritten.
- If a remote write is uncertain, the cycle exits 75. It does not retry, roll back
  or release the lease; the next cycle reconciles from Drive.
- The local markers that gate the engines are never uploaded.

**Proof** (`e123-cycle-proof.yml`, runs on main)
- Uses real Drive and real Gemini on a random synthetic prefix
  `99_INBOX/CANDIDATES/E123_CYCLE_PROOF/<random>`.
- Runs two cycles, each in a fresh mirror.
- Checks: settlement, idempotency, lease FREE, sources untouched.
- On PASS it purges that prefix only.

**Deterministic coverage** (`tests/test_e123_cloud_cycle.py`)
- CREATE, REJECT, UPDATE (same RECORD_ID, old text preserved), UNCHANGED.
- Empty day, an uncertain write, recovery of the pre-write lease, a started-but-uncommitted stop.

**Known item for Marco 4**: E1 refuses to replace an existing
`FILE_EVIDENCE/index.json` that has entries it did not create (`foreign_evidence_index`).
Before activation, check the real Drive state of that folder.
