# Technology Library — Project Charter

CHARTER_VERSION: 1.1
STATUS: ACTIVE  
LAST_UPDATED: 2026-11-01

## North Star

The user can drop interesting files, links, videos, images, documents or other supported material into the private Google Drive inbox and walk away. The system automatically decides what is useful, processes it, rejects/archives noise and accidental uploads, deduplicates, consolidates knowledge, updates the canonical Technology Library when justified, and makes the result selectively retrievable by ChatGPT later.

Routine ingestion and knowledge promotion must not require item-by-item, candidate-by-candidate or batch-by-batch human validation.

## Problem

Interesting technical information arrives in messy formats and is easy to lose, duplicate, forget or repeatedly re-analyze. The project exists to turn that stream into structured, reusable knowledge automatically and cheaply.

## Black-box success test

V1 acceptance requires seven consecutive real scheduled operating days under the
V1.2.0 specification, section 7. Inputs include useful sources, duplicates,
irrelevant or accidental uploads, weak evidence and malformed/unsupported items.
The 500-item scale test is outside V1; it is not a substitute for real operation.

Without the user triggering processing, reviewing candidates, approving batches, keeping a PC on or manually publishing records, the system must:

1. detect the backlog automatically;
2. process it in bounded resumable batches;
3. classify each item into an appropriate automatic disposition;
4. extract grounded evidence from useful material;
5. deduplicate and cluster overlapping information;
6. compare extracted knowledge with the current canonical Library;
7. automatically CREATE, UPDATE or leave UNCHANGED canonical records when policy allows;
8. keep weak/conflicting/unsafe material outside canonical state without turning it into a user task;
9. rebuild and verify indexes automatically after canonical changes;
10. recover automatically from ordinary interruption/failure;
11. leave the resulting knowledge retrievable through the normal Technology Library routing path;
12. keep private media, credentials and extracted private content out of the public code repository and public logs;
13. require R$ 0 mandatory additional recurring cost under the intended normal operating mode.

The seven-day run must demonstrate all of these observable cases:

- an empty day completes E1, E2 and E3 without a new lot or error;
- a useful new item produces CREATE, rebuilt/verified indexes and normal routed retrieval;
- new knowledge produces UPDATE with the same RECORD_ID and preserved valid older claims;
- a duplicate produces UNCHANGED without a new record or reanalysis;
- irrelevant/accidental content produces automatic REJECT;
- weak evidence produces automatic HOLD without a user task;
- an ordinary model/quota/Actions failure recovers in the following cycle without loss or duplication;
- Drive credentials work for more than eight days without manual renewal;
- public logs/artifacts expose no private filename, identifier or content.

Synthetic or local-only runs cannot satisfy these real operating-day criteria.
Private receipts, recovery evidence and routing readback must support acceptance;
only a sanitized aggregate proof and evidence-manifest digest may be public.

The project is not complete until this black-box test passes.

## User must not have to

- manually trigger ordinary Drive processing;
- keep the personal computer powered on for normal operation;
- inspect every uploaded source;
- review every candidate;
- approve every batch;
- authorize each ordinary canonical publication;
- manually deduplicate or organize information;
- manually decide that an irrelevant/accidental upload has no value;
- manually repair ordinary interrupted runs.

## Non-negotiables

- Google Drive remains the private inbox/storage authority for user media and private processing state.
- `00_LIBRARY` remains the canonical knowledge authority.
- Normal operation targets R$ 0 mandatory additional recurring cost.
- No paid model/API fallback is required for the core path.
- Private user content, Drive credentials, transcripts, OCR output and extracted evidence must not enter public Git history or public Actions logs/artifacts.
- The normal path must operate without the user's PC.
- Processing must be bounded, resumable and fail closed.
- Low-confidence or conflicting information may be held automatically rather than promoted.
- HELD is a machine state, not a review queue the user is expected to clear.
- Ordinary canonical promotion is policy-driven and automatic once evidence/confidence/safety gates pass.
- Safety must be implemented through evidence rules, confidence thresholds, reversible transactions, snapshots, rollback, recovery and quarantine, not by quietly reintroducing routine human approval.
- Destructive deletion of private source material is not required for zero-touch operation; automatic archive/status transitions are preferred unless an explicit retention policy authorizes deletion.

## Allowed manual actions

Manual action is acceptable for exceptional or administrative work such as:

- first-time credential/setup changes;
- changing project policy or the Charter;
- rotating compromised/expired credentials;
- exceptional recovery only when automated recovery proves unable to restore a safe state;
- intentional inspection requested by the user;
- repository visibility/security administration that cannot be safely automated.

These exceptions must not become part of the routine ingestion flow.

## Forbidden drift

Without an explicit Charter revision approved by the user, do not:

- introduce recurring human review;
- introduce recurring publication approval;
- require a local always-on machine;
- add mandatory recurring cost;
- redefine safety to mean human must inspect it;
- declare the project complete because internal stages/tests are green while the black-box user experience still fails;
- treat approval of an implementation step as approval of a product-scope change.

## Architecture freedom

Implementation details may change freely when they preserve this Charter. Examples include GitHub Actions, rclone, n8n, Apps Script, local models, different classifiers or new storage adapters.

Tools are replaceable. The user experience and constraints above are not.

## Scope change policy

A true change to the product objective requires all of:

1. an explicit proposed change;
2. a comparison against the current Charter;
3. explicit user approval of the changed objective;
4. a Charter version increment;
5. roadmap and contract updates before implementation.

Silence, convenience, a local technical decision or approval of a sub-stage does not count as scope approval.

## Approved acceptance revision — 1.0 to 1.1

- Proposed change: replace only the V1 500-input scale threshold with the seven
  consecutive real-day acceptance matrix above; retain automatic publication,
  recovery, retrieval, privacy, zero routine human steps, no user PC and R$0.
- Comparison: Charter 1.0 required the same end-to-end product experience plus
  500 inputs. Charter 1.1 preserves that experience and all non-negotiables;
  the scale test moves outside V1, while real scheduled operation, next-cycle
  recovery and credentials lasting more than eight days remain blocking.
- Owner approval provenance: the owner-approved V1.2.0 specification dated
  2026-10-19 explicitly excludes the 500-item test and defines section 7 acceptance.
  The owner's executive resumption request of 2026-11-01 explicitly directs
  formal reconciliation under this scope-change policy and repeats these criteria.
  This revision records that decision, not inferred approval from a technical PR.
- Formal implementation: increment Charter to 1.1 and update the contract,
  five-milestone roadmap and alignment guard together before production activation.
- Acceptance remains NOT_RUN. This revision is not evidence of completion.

## Definition of done

The Technology Library project is DONE only when the black-box success test passes and all non-negotiables remain true.

A core subsystem release may be tagged independently, but must be named as a subsystem/core milestone rather than implying the North Star is complete.