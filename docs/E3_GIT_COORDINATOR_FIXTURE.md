# E3: Git exact-ref coordinator with synthetic transaction recovery

Status: **implemented and tested ONLY on isolated local synthetic files and a local bare Git repository**. Not scheduled, not deployed, no real Drive writes.

## Why this increment exists

The canonical transaction, durable journal, index rebuild and rollback engines were already present. A transaction can still corrupt knowledge if two processes own the same write path, or if a timed-out Git push is treated as a harmless failure. This increment connects those existing components under a single verified Git compare-and-swap ownership flow in a local fixture.

- Reuse `candidate_production_coordinator` and its dedicated coordinator ref. The fixture's Git origin MUST be an existing bare repository nested inside the marked synthetic test root, not a GitHub URL.
- Acquire an exact owner for transaction ID, batch ID, and the journal path; verify its ACTIVE state and identity by rereading the ref.
- Use `e3_recovery_fixture` to create snapshots with readback and a PREPARED journal before crossing the Git coordinator write boundary.
- Mark the exact coordinator ACTIVE owner as having crossed the canonical write boundary, verify the ref, and only then write the durable WRITING_RECORDS journal and invoke the existing mutation/index engine on synthetic files.
- On incomplete or failed mutation, remain ACTIVE and retain snapshots. A different owner/transaction cannot take over.
- In a later invocation, validate the exact active owner, inspect the journal, revalidate snapshots and pre/post bytes with the existing production recovery engine, persist RECOVERED and verify its bytes.
- Release the Git ref only by the `release_recovered` CAS transition with the SHA-256 of the verified recovered journal. Verify resulting FREE state, matching released transaction and receipt hash.
- An ambiguous PREPARED journal after a coordinator write-boundary transition is **NOT** interpreted as safely aborted. It remains blocked for durable reconciliation; automatically releasing there would be unsafe.
- Existing coordinator Git subprocess calls now have a 40-second timeout. A timed-out push is an uncertain outcome, not proof the ref was unchanged, and the fixture does not continue mutating or automatically retrying release.
- This adapter has **no CLI**, no Google Drive or real GitHub execution path; no new credentials or cost.

## CI acceptance

`python3 -m unittest tests.test_e3_coordinator_fixture -q`

Covers verified rollback+owner release, partial write with recovery on another invocation, rejection of a concurrent owner, foreign canonical modification refusing rollback, journal corruption, mismatched ownership, rejection of nonfixture Git URL/paths, no-write fast path without lease, and bounded Git timeout. The full PR CI also executes the E1/E2/E3 and cost/privacy guardrails.

## Still blocked before running on real data

1. Production worker must acquire Git coordinator ownership before touching private Drive state, including a durable fencing protocol for GitHub Actions run cancellation/timeout so a stale runner cannot start a delayed write.
2. All private snapshots, journals, and receipt transitions must use persistent Drive readback, safe uncertain-write reconciliation and idempotent commit/recovery. The current immutable private transport library is **not** a full CAS journal updater.
3. Production E3 must apply actual CREATE/UPDATE with write-order and transaction lease enforcement. Commit and terminal sidecar need durable readback; no release of coordinator before this.
4. Test crash/restart and concurrent runners against private **synthetic Drive fixtures**, including a lost acknowledgement on a completed remote write, before enabling real DROP_HERE.
5. Independently review the stacked PRs #11 → #12 → #13 and audit E2's live Gemini API privacy, request limits, model availability and R$0 criterion.
6. Run the required real, scheduled seven-day V1 acceptance after full end-to-end integration.

ALIGNMENT: PASS for isolated fixture only.
NEW_ROUTINE_USER_STEPS: 0.
NEW_MANDATORY_RECURRING_COST: R$0.
USER_PC_DEPENDENCY: no for intended production.
BLACK_BOX_STATUS: NOT_RUN.
