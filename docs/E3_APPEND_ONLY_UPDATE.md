# E3 automatic append-only UPDATE of canonical knowledge

Status: inactive code, no production writer enabled. This change repairs an
otherwise blocking E2/E3 mismatch: existing canonical records can have
STATUS: REFERENCE, whereas E2's proposal template uses STATUS: TEST. Replacing
a complete record with the proposal could strip old knowledge or downgrade
status. Requiring the old bytes to be verbatim in the model proposal was
impractical when E2 only sees compact evidence and match summaries.

## Deterministic policy

For UPDATE, E3 validates E2's exact matched RECORD_ID, TYPE, route and title
against the *actual* existing canonical record. E3 ignores the model-authored
replacement prose. It composes a new canonical record from the original bytes,
preserving all previous headers, status and text, and appends only the
`claims` whose quotes were already grounded in compact evidence by E2.

The resulting record is revalidated using the canonical Markdown validator,
is still subject to an EXACT_BASE_SHA precondition and the existing
transaction/index/rollback pipeline. Claims are single-line inert Markdown
text; unsafe delimiter characters and duplicate/existing claims block rather
than silently overwrite content.

The current gate remains conservative: duplicated facts or malformed
evidence cause a transaction preflight failure, never an unsafe publication.
E2 and scheduling still need machine disposition/retry handling for these
cases, without adding recurring human review.

## Acceptance checks

- Original REFERENCE status, record identity and all content preserved.
- Proposed replacement prose never written.
- Actual verified transaction and existing receipt engine accept UPDATE.
- Evidence excerpt remains attached to new claim.
- Existing claims and Markdown injection characters are rejected.
- No data or code path in this change invokes real Drive writes.

This improves the current Marco 2 code contract; it is NOT a pass for
production recovery, automatic canonical promotion or the seven-day V1
acceptance.

BLACK_BOX_STATUS: NOT_RUN
