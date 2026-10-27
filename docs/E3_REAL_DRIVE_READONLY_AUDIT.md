# E3: read-only readiness inspection of real private Library

Status: added as a one-shot, read-only diagnostic gate inside Marco 2. It does
not activate the E3 production writer or the DROP_HERE intake.

## Actual operation

The workflow `.github/workflows/e3-live-readonly-audit.yml` runs once on
its first merge to main and supports manual `workflow_dispatch`. It uses the
existing secret TL_RCLONE_CONFIG through a temporary rclone configuration.
It has only read-only implementation operations (rclone `lsjson`/`cat`).
No remote writes, deletes, moves, model calls, Git CAS state changes, sources,
private decisions, names, or document bytes are printed or uploaded.

The audit inventories real canonical records, rereads each bounded Markdown
file and MASTER_INDEX.md, regenerates the expected index bytes with
`build_index_plan`, and checks the actual index bytes. If record format,
index, remote listing, file size, or access is invalid, it fails closed with
stable status codes only. It enforces a maximum of 160 canonical records for
this isolated audit, not for eventual V1 scale.

Optional manual `inspect_pending=true` also selects the oldest unreceipted
lot with existing E2 decisions, reads its bounded private bytes, and simulates
E3 in a disposable local mirror. Neither remote LOTES nor canonical files
are modified.

## Receipt-compatibility regression

E3's transaction plans now use `lane=NEW` for CREATE and `lane=UPDATE`
for UPDATE, and include the original package_id/revision_key/candidate_id.
This aligns E3 plan metadata with the existing
`candidate_transaction_receipt.build_receipt` validator. Tests exercise
CREATE, UPDATE, and intentional rejection of the earlier generic E3 lane,
all without remote writes.

## Limits

Passing this audit means only the current Library can be read and its generated
indexes are consistent. It does not prove Git production fencing, durable
snapshot transitions, external timeout reconciliation, live CREATE/UPDATE,
or seven days of hands-off processing.

If the existing Library has independently edited indexes, this diagnostic may
fail; that is a useful block, not authorization to overwrite indexes. Any
repair needs a separate evidence-preserving reversible transaction under E3.

ALIGNMENT: PASS for read-only scope.
CHARTER_VERSION: 1.0.
NEW_ROUTINE_USER_STEPS: 0.
NEW_MANDATORY_RECURRING_COST: R$0.
USER_PC_DEPENDENCY: none.
BLACK_BOX_STATUS: NOT_RUN.
