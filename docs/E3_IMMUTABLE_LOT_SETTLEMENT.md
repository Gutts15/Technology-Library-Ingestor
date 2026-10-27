# E1–E3 immutable-lot reconciliation and private E3 transport

Status: synthetic integration verified in CI; production orchestration NOT ACTIVATED.

## Why the sidecar exists

E2 binds decisions to the exact serialized lote.json bytes. Editing lote.status after E3 changes those bytes and breaks SHA-256 integrity. E3 instead writes an immutable receipt named recibo.json (or recibo-02.json for a sequenced lot). The original lote.json stays PRONTO and byte-for-byte identical. The derived machine state is APPLIED only when the receipt is fully valid.

## E1 resume rule

- Before creating a new lot, E1 inventories past lots and terminal sidecars using the same bounded private rclone listing.
- Each prior immutable lot contributes its item IDs to the seen set, whether pending or settled; no duplicate E1 selection.
- A PRONTO lot without receipt is still pending. E1 creates no additional lot until that state resolves.
- A receipt is valid only if lote_id, serialized lote_sha256 and serialized decisoes_sha256 match, decisions/result item IDs and actions match 1:1, write-result hashes are structurally valid, and any canonical write has the committed transaction and index verification proofs.
- If receipt exists but decisions are missing, corrupt, duplicated, inconsistent or hashes conflict, E1 fails closed. It does not delete, rewrite or silently bypass it.
- HELD and REJECT are machine results, not human review tasks; a verified nonwrite receipt allows the next lot.
- For CREATE or UPDATE a sidecar is authorized only after the separately proven, committed canonical transaction. This adapter does NOT yet write those receipts.

## Transport boundary

src/e3_private_write.py supports only immutable private receipt and production-recovery snapshot / initial journal targets, with bounded rclone calls, no API credential handling and no console output of contents. It never writes canonical 00_LIBRARY and does not expose a CLI. A failed/timeout copy is UNCERTAIN, never retried automatically; a separate read-only reconcile verifies the expected bytes. An inaccessible destination is UNCERTAIN, not assumed missing.

The transport is NOT a transaction coordinator, does not apply records, and does not implement atomic journal replacement. The production Git exact-ref coordinator still needs to gate every canonical mutation. A timeout of a mutating remote call cannot be interpreted as permission to release ownership.

## Verified synthetic integration

tests/test_e123_synthetic_integration.py executes an actual local E1 -> E2 -> E3 HOLD workflow and verifies that a second E1 cycle selects only unseen material; invalid actions and missing receipts stop progress. Additional tests cover sidecar binding, duplicate JSON keys, digest mismatch, mismatched outcomes, transport timeouts and read-only reconciliation. CI uses only synthetic data, no Drive remote, no Gemini key, no paid API.

## Remaining gates (mandatory)

1. Independent review of stacked PRs #11, #12, #13. No automatic merge without review.
2. Implement remote E3 exact-ref coordinator ownership, durable journal transitions, private snapshots, record writes and commit recovery in one executable job, including uncertain-response reconciliation. Prefer reuse of candidate_production_coordinator and existing transaction engines.
3. Persist readback-verified terminal receipts for committed CREATE/UPDATE; settle disposition idempotently. Ensure reconciliation does not skip conflicted transactions.
4. Integrate E1/E2/E3 into the currently fixture-only workflow; carry through private Drive, approved free Gemini tier, and index rebuild.
5. Run synthetic end-to-end with CREATE/UPDATE and resume, then the required seven-day real scheduled acceptance, zero additional cost and full privacy audit.

This component is not a declaration of E3 production readiness or V1 completion.

ALIGNMENT: PASS for isolated integration.
CHARTER_VERSION: 1.0.
NEW_ROUTINE_USER_STEPS: 0.
NEW_MANDATORY_RECURRING_COST: R$ 0.
USER_PC_DEPENDENCY: no for intended normal operation.
BLACK_BOX_STATUS: NOT_RUN.
