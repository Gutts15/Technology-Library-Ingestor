# E3: Transaction preflight and offline canonical simulation

This is an INACTIVE safety increment, stacked on E2 PR #12, which is stacked on E1 PR #11. No production wiring, no Drive remote writes or canonical publication. It does not complete E3 or the V1.

## Coverage

- Re-read the serialized lote.json and decisoes.json and confirm lote_sha256, item-list binding, action contracts and exact evidence quotation.
- Verify current canonical records and generated MASTER_INDEX.md are consistent; fail closed on stale indexes.
- Reject UPDATE for an unknown or mismatching RECORD_ID; reject CREATE when the canonical title, target path or RECORD_ID collides, even if E1 matches were incomplete.
- Validate complete Markdown headers and target paths using the existing library_index_build.validate_record.
- Keep existing UPDATE content verbatim; block any attempted removal. This conservative gate deliberately requires additional E2 prompt work for real UPDATE cases.
- Reject RESEARCH publication: existing canonical index builder only supports TECHNOLOGY, PATTERN, PIPELINE and SOURCE. RESEARCH needs separate routing policy, not accidental promotion.
- Compile a transaction plan in the format expected by candidate_canonical_transaction_engine and run the actual engine with an in-memory overlay. Verify rebuilt indexes, changes and target bytes without persisting any output to the private Drive or public repository.
- No model call, no mutable local file, no canonical write, no receipts written, no automatic settlement, no source deletion. Public CLI output includes fixed codes only.

Run synthetic offline verification:

    python3 -m unittest tests.test_e3_transaction_preflight -q

CLI local-only, read-only:

    python3 src/e3_transaction_preflight.py --root-dir ./synthetic-private-fixture --date-brt 2026-10-27

## Still needed before active E3

1. Integrate the existing coordinator with durable prewrite journal, private snapshots, ownership, recovery and uncertainty handling, before permitting any real canonical write.
2. Verify batch-wide receipt semantics, order and idempotent settlement for REJECT, HOLD and UNCHANGED and persist status APLICADO/REJEITADO safely.
3. Integrate Actions E1/E2/E3 only after reviewed code and quota/cost gates. Preserve --fixture-only for current live workflow until synthetic end-to-end passes.
4. Run a genuine private synthetic end-to-end acceptance, then seven scheduled days as specified. Nothing in this PR counts as the V1 acceptance.
5. A source may be textually quoted yet semantically misrepresented. Quote checks are necessary but not sufficient to authorize canonical publication.

ALIGNMENT: PASS for *offline preflight only*
CHARTER_VERSION: see active PROJECT_CHARTER
NEW_ROUTINE_USER_STEPS: 0
NEW_MANDATORY_RECURRING_COST: R$ 0
USER_PC_DEPENDENCY: no
BLACK_BOX_STATUS: NOT_RUN
