# E3: isolated durable recovery fixture (NOT production)

Status: development test only. E3 is not activated and private Drive data is untouched.

The fixture reuses existing components:
- candidate_transaction_prewrite_arm.build_prepared_journal
- candidate_canonical_transaction_engine.execute_canonical_transaction
- candidate_production_recovery_engine.execute_recovery

It requires a synthetic-only marker with bytes E3_SYNTHETIC_ONLY plus newline under 99_INBOX/CURATION/_synthetic-e3-fixture.marker. It only accepts --root-dir, never --remote. It cannot invoke Gemini or delete real sources.

## What is exercised

1. Preflight checks lot-decision bindings, canonical index consistency, ID/route collisions and quoted evidence.
2. Computes expected post-transaction state in memory, snapshots pre-existing synthetic records and indexes to a private local recovery folder, and verifies snapshot bytes.
3. Persists a PREPARED journal before publishing anything and a WRITING_RECORDS journal before any local synthetic mutation.
4. Executes the existing canonical record and index engine against marked synthetic local files.
5. Restores original local files with the existing recovery engine, verifying the full restore before recording RECOVERED.
6. Tests a partial mutation and a failure during index writing; a subsequent invocation can restore the synthetic state using durable snapshots.
7. Rejects external divergent changes instead of overwriting them during recovery.
8. Blocks replay of a transaction that already has a journal.

Offline verification: python3 -m unittest tests.test_e3_recovery_fixture -q.

## Remaining production blockers

- Local flock is not a durable remote GitHub/Drive coordinator lease.
- Production snapshots and journals require private Drive persistence and readback, plus uncertain-write reconciliation before any retry.
- Verified production changes need a durable receipt and idempotent lot-status settlement. The fixture deliberately restores synthetic baseline even after successful execution.
- Do not treat a lost receipt response as evidence that canonical writes failed.
- Durable machine outcomes for UNCHANGED, REJECT and HOLD remain to be integrated.
- V1.2 specifies a hash-bound immutable lote.json while also requesting a status update to the lot after E3. Resolve that tension with a versioned settlement sidecar or transaction-safe terminal marker before activating writes, without silently invalidating E2 hashes.
- Evidence quotes alone cannot prove the semantic correctness of a record; additional conservative checks remain necessary.
- There is no live Gemini decision in E2, no integrated live E1/E2/E3, and no seven-day acceptance yet.

ALIGNMENT: PASS for this isolated fixture.
CHARTER_VERSION: 1.0.
NEW_ROUTINE_USER_STEPS: 0.
NEW_MANDATORY_RECURRING_COST: R$ 0.
USER_PC_DEPENDENCY: no for intended operations; local fixture exists only for CI.
BLACK_BOX_STATUS: NOT_RUN.
