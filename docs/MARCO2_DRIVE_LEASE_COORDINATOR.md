# Marco 2: private Drive lease for the existing production coordinator

Decision (André, 2026-11-01): the production coordinator state lives in the
**private Drive**, not in a public GitHub ref. The production job keeps
`permissions: contents: read`; no job holding the Drive secret gets repository
write access.

Diagnosis that led here: `refs/heads/tl-candidate-production-coordinator` did
not exist (`matching-refs/heads/tl-` empty), there were no rulesets and `main`
had no protection. The 404 meant *absent resource*, not a GitHub restriction.
Creating/using the ref would require `contents: write` in a public repository,
which was declined as unnecessary attack surface.

## Design

- Same module, same API, same state schema, same transitions
  (`candidate_production_coordinator.py`). Only the transport changes when
  `remote_url` starts with `drive-lease:<rclone-dir>`.
- Observed version = SHA-256 of the exact state bytes. Every transition
  re-reads and compares the version immediately before its single write, then
  verifies by readback (as the Git CAS + readback did).
- Exclusivity between runs: GitHub Actions concurrency group
  `technology-library-private-storage-ingest`, `cancel-in-progress: false`
  (one run at a time). The lease adds exact-owner identity, generation and
  stale-write fencing on top.
- Uncertain write (lost acknowledgement) is never retried; callers re-read,
  exactly as with a lost Git push acknowledgement. Unreachable Drive fails closed.

Residual limit, stated explicitly: Drive has no atomic compare-and-swap, so the
lease alone does not stop two *simultaneous* writers; that guarantee comes from
the Actions concurrency group, which all private-storage workflows share.

## Evidence

- `tests/test_coordinator_drive_lease.py`: exclusivity, second writer
  rejected, same-owner resume, write boundary, committed release, lost ACK,
  dropped write, stale-base and late-write fencing, unreachable Drive.
- `.github/workflows/coordinator-drive-lease-proof.yml`: real private Drive,
  fresh synthetic prefix `99_INBOX/CANDIDATES/COORDINATOR_LEASE_PROOF/<random>/`,
  status codes only in logs.
