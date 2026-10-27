# E2 Gemini decision preparation: isolated draft

This module implements the decision-file boundary of the approved Technology Library V1.2.0 architecture. It is not scheduled, not deployed, and does not activate the private inbox.

## Operational contract

- Find the oldest private E1 lote still PRONTO without a decision file.
- Verify the lote identifier, item bindings, item-list SHA, 3 KB per item, item count and allowed evidence fields.
- Call the pinned Gemini Flash-Lite model only with an explicit --call-gemini option, one request per item (maximum 20 for a lote). No SDK, paid fallback, grounding, web tools or API retry; HTTP quota/payment blocks stop with a fixed code.
- Require one decision per item using CREATE, UPDATE, UNCHANGED, REJECT or HOLD; validate exact identifiers, unique decisions and complete response.
- Require CREATE/UPDATE claims to include verbatim supporting quotes from compact evidence. Confidence must be at least 0.8. UPDATE can only target a matched RECORD_ID. CREATE is blocked if there are matches.
- For write decisions require draft Markdown with RECORD_ID, TYPE, STATUS: TEST, DOMAIN and CATEGORY headers; E3 still performs canonical semantic and transaction checks.
- When all items validate, emit one decisions payload with the SHA-256 of the original serialized lote.json. E3 must compare this digest to the actual lot bytes before applying anything.
- --apply uses private LOTES only; an existing decision file is never overwritten. No canonical writes, index rebuild, moves, deletions, or status changes.

The output filename follows the lot sequence: lote.json -> decisoes.json; lote-02.json -> decisoes-02.json. Multiple lots are not processed in a single invocation.

## Local synthetic test only

Read-only:

    python3 src/e2_gemini_decide.py --root-dir ./private-fixture --response-json ./stub-decisions.json

Isolated local output:

    python3 src/e2_gemini_decide.py --root-dir ./private-fixture --response-json ./stub-decisions.json --apply

For future production deployment, --call-gemini needs the previously configured repository GEMINI_API_KEY. Do not run with --remote --apply against real data until E3, operational scheduling, quota budgets and privacy gates have been reviewed and accepted.

## Safety and limitations

- Offline CI uses synthetic responses only; no actual Gemini API calls in this PR.
- A single rate-limit, invalid JSON, unsupported record action or weak evidence blocks writing the entire decisions file. The lot remains PRONTO and is retried by a future orchestrator.
- E2 validators prove exact quote occurrence and identifiers, not the semantic truth of the Markdown record. E3 must enforce stronger record-level policies, duplicate detection, canonical lookup, snapshots and rollback.
- E2 does not yet enforce actual Free Tier billing-state inspection. The same first-time user configuration and R$0 rule from G0.3b apply.
- No workflow calls E2 yet. It is an isolated development PR stacked on E1 PR #11. Rebase or retarget after E1 is reviewed; do not merge E2 out of order.
- The V1 seven-day black-box acceptance has NOT RUN.
