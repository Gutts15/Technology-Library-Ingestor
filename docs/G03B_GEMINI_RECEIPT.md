# G0.3b: isolated Gemini free-tier receipt

Implements only the storage/model availability gate from the approved v1.2.0
specification. It does not implement ingestion, decisions or publication.

## Boundaries

- Standard public Actions runner, daily at approximately 08:00 UTC (05:00 Brasília, UTC-3); manual
  dispatch is also available after merge. GitHub may delay scheduled runs.
- Only `contents: read`; checkout credentials are not persisted. No artifacts.
- Requires the existing repository secrets `TL_RCLONE_CONFIG` and
  `GEMINI_API_KEY`. Empty/missing secrets stop before any external call.
- Reads only the pinned synthetic input in the isolated test folder. Unexpected
  bytes fail before any model call. No real inbox or canonical storage access.
- Uses `gemini-3.5-flash-lite`, one `generateContent` request, JSON schema,
  128 output tokens maximum, no tools, grounding, fallback, or API retries.
- Official pricing checked **2026-10-19**: standard input and output are free
  on the Free Tier. The API key's project must remain on Free Tier. This code
  cannot inspect account billing tier from an API key and never changes billing.
  A key belonging to an already-paid project is not a free-tier guarantee.
- Quota, access, model, billing and network failures stop the run. No upgrade,
  payment, alternate provider, or human approval path is implemented.
- Only direct test-folder listing plus input/receipt reads and a single receipt
  create attempt. No Drive deletion/move/update command. Local ephemeral files
  are removed when the runner's temporary-directory context ends.
- Same-day receipts are never overwritten, including malformed/conflicting
  receipts. Actions concurrency serializes this workflow; `--immutable` and
  `--ignore-existing` add protection. Google Drive has no path-level atomic
  create-if-absent through rclone: unrelated external writers must not write to
  this receipt namespace. Duplicate names fail closed and are never cleaned up.
- Writes are not retried. A failed/timed-out create gets one listing/readback
  reconciliation; if exact bytes cannot be confirmed, the run fails.
- Public application logs contain fixed status codes and counts only. Provider
  error bodies, content, credentials, remote paths and Drive IDs are suppressed.

## Acceptance after review and merge

Run one manual dispatch as the initial connectivity check. Then require three
distinct consecutive **BRT dates** with successful **scheduled** runs and byte
readback. A receipt alone is insufficient: match it to the successful run.
The receipt records event type, run/attempt, code revision, input digest, model,
response digest/byte count and actual token usage (null if not returned).
Active RPM/TPM/RPD limits are project-specific and visible in AI Studio; no
unobserved numeric quota is claimed. This gate makes at most one request/run.

A manual receipt does not count as a scheduled day. If manual and scheduled
runs share a date, preserve the first receipt; the scheduled window starts
with subsequent eligible dates. Repeats verify the existing receipt and make
zero new model calls/writes. Never backfill dates or count reruns as new days.

## Verification

`python3 -m unittest tests.test_g03b_gemini_receipt tests.test_cost_guardrail_g03b -v`

The cost guardrail exception is limited to this workflow's named Gemini key.
Other workflows/providers and `GOOGLE_API_KEY` remain blocked.

Official references:

- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite
- https://ai.google.dev/api/generate-content
- https://ai.google.dev/gemini-api/docs/rate-limits
- https://ai.google.dev/gemini-api/docs/billing
