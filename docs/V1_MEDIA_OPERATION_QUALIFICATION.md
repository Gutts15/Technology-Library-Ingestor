# V1 media and daily-operation qualification

This document describes remaining work inside approved milestones 3 and 4. It does not add milestones, enable intake, or claim production acceptance.

## Milestone 3: actual extraction and local integrated qualification

`e1_source_extract.extract_file` invokes the existing processors in a bounded
Linux process group: input 32 MiB, artifacts 64 MiB/4096 entries, compact evidence
and one shared wall deadline. Decoder descendants are killed before outputs are
read; credentials and decoder stdout/stderr are excluded. It never accesses Drive
or downloads a speech model.

Actual encoded inputs generated during tests exercise text, DOCX XML, PDF through
pdftotext, CSV, XLSX through openpyxl, PNG OCR and MP4 frame OCR through
FFmpeg/Tesseract. These are real decoder invocations over generated inputs, not
the user's backlog or live Gemini classification. Missing tools cause explicit
test skips; Linux Security gate installs media and spreadsheet dependencies.

`e1_source_prepare.prepare_sources` binds outputs to existing envelope/index/lot
formats in a marked local private mirror. Content-addressed receipts prevent
renamed duplicates from being decoded again and persist HELD outcomes. Foreign
existing evidence is rejected before replacing its index. Originals are preserved.
This local adapter does not establish shared cloud coordination or deployment.

The integration test decodes a text file, prepares E1, supplies offline E2 decisions
and executes CREATE followed by identity-preserving UPDATE through the existing
durable E3 engine and local bare Git coordinator. It checks terminal receipts,
master/domain routing, preserved older content and repeat without model calls or
re-extraction. All generated inputs/artifacts remain temporary.

Audio remains HELD without an explicitly supplied local speech model. Unverified
links remain HELD rather than treating a URL as extracted content. Real speech
transcription, fetched link evidence, live Gemini CREATE/UPDATE and the shared
Drive/GitHub chain are still unproved V1 requirements.

E2 now bounds its entire model exchange in a reaped subprocess, including missing
or partial bodies, and shares a five-minute budget across the lot. Keys/evidence
pass through stdin, never command arguments. Failure leaves the original lot and
writes no partial decisions. No retry or paid fallback is introduced.

## Existing synthetic disposition matrix

The synthetic integration matrix exercises seven evidence kinds through E1, offline E2 decisions, the E3 immutable terminal sidecar, settlement verification and the next cycle. Each kind covers HOLD/WEAK_EVIDENCE, REJECT/IRRELEVANT and UNCHANGED/DUPLICATE: 21 scenarios. It checks no repeated model call, no repeated lot and unchanged canonical master bytes. The decisions are supplied fixtures; the test does not establish model classification quality.

| Evidence kind | Input to the matrix | Still required for end-to-end media acceptance |
| --- | --- | --- |
| Text | extracted samples | bounded real extraction, grounded CREATE/UPDATE, normal retrieval |
| Document | extracted samples | representative PDF/document extraction and malformed-file isolation |
| Image | OCR sample | representative OCR accuracy and claim support |
| Spreadsheet | textual row samples | bounded workbook/CSV extraction and claim support |
| Audio | high-signal speech sample | actual transcription accuracy, limits and weak-signal handling |
| Video | OCR sample | actual visual/speech extraction, signal filtering and supported limits |
| Link | extracted public text | actual public-source retrieval and inaccessible-link disposition |

An evidence kind is not a file-format support guarantee. ZIP/RAR extraction and legacy formats excluded by the approved V1 specification remain outside this proof. No private source content is required in this repository. Availability-gate tests separately cover empty/malformed evidence, weak speech and video OCR excluding weak speech.

## Milestone 4: activation dependencies

The current daily zero-touch worker remains fixture-only. E1 and E2 are standalone components; the integrated E3 commit adapter is explicitly a synthetic fixture using a local bare Git coordinator. A successful Drive mirror test does not turn that local coordinator into the shared GitHub coordinator.

Production remains blocked until an authorized shared-coordinator execution proves ownership acquisition, competing-writer exclusion, journal/receipt persistence, readbacks, lost acknowledgement and fresh-process restart over Drive and Git in the same chain. Existing connection restrictions must not be bypassed by a different token, workflow, tool or transport.

After that proof, qualify one bounded cloud chain using the existing E1/E2/E3 components, one canonical writer, persistent recovery before new work, immutable lot/decision/receipt bindings and no repeat of uncertain writes. Keep the original source, preserve snapshots and stop mutations on conflicting hashes or ownership. The user must not become the recovery worker.

Before activation verify that actual E1/E2/E3 invocations share the intended serialization and recovery boundary; the isolated acceptance workflows have separate concurrency groups and are not a production concurrency proof. No change to these workflows or their permissions is part of this qualification.

## Finite completion criteria

Use the approved V1.2.0 acceptance: seven consecutive real scheduled days, all required dispositions, grounded CREATE and identity-preserving UPDATE, repeat without reanalysis, ordinary-failure recovery, normal index routing/retrieval, Drive credentials surviving more than eight days without manual renewal, and no private data in public logs/artifacts. Charter 1.1 formally records the owner-approved replacement of the 500-item scale test with this real acceptance matrix. Contract, roadmap and alignment guard are updated together; the North Star and all hard constraints remain intact. No additional milestone is introduced.

No mandatory recurring charge, recurring human approval or always-on user PC is introduced. BLACK_BOX_STATUS remains NOT_RUN.

## Verification

Run the existing Security gate and Project alignment guard on the exact PR head. The media matrix is included in the already configured `tests.test_e123_synthetic_integration` invocation, so no new workflow or permission is needed. Linux-only durable recovery fixtures must pass on the existing Linux CI; importing them on Windows fails because `fcntl` is absent. Do not stub the lock implementation to turn that environment limitation into a passing result.
