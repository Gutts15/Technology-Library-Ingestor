# Stage 21 — Evidence availability gate

Stage 21 remains IN_PROGRESS. This increment supplies a deterministic gate at
automatic FILE_EVIDENCE candidate creation; it does not enable real intake.

Each new private envelope records an `evidence_assessment` containing a policy
version, ELIGIBLE/HELD state, allowed/blocked channel names and fixed reason codes.
The candidate bridge recomputes the assessment from the hash-bound summary and
rejects a changed assessment. Legacy envelopes are assessed without rewriting.

- Video: requires nonempty OCR or speech with medium/high transcript signal.
- Audio: requires nonempty speech with medium/high transcript signal.
- Image: requires a nonempty OCR sample.
- Text/document: requires nonempty extracted samples.
- Spreadsheet: requires nonempty textual row samples.
- Link: may proceed to the existing verified public-source fetch.
- Unknown type, empty/malformed evidence, or weak/unverified speech alone: HELD
  before any fetch, model call or candidate creation.

When usable OCR accompanies weak speech, only OCR is supplied to candidate
analysis. HELD is a machine disposition, not a mandatory human review queue.
These signals indicate available evidence, not verified semantic correctness;
relevance, claim grounding, consolidation and canonical promotion still require
their later gates. Existing completed candidate receipts remain idempotent.

Acceptance uses synthetic fixtures covering empty content across kinds, usable
text, low/unknown/malformed speech signals, mixed OCR/speech, changed assessment
and legacy envelope compatibility. No live Drive content is used.
