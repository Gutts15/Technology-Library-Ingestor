# Stage 21 — Synthetic raw-file integration proof

Checkpoint: 2026-10-08. Local result: 12/12 PASS, 13.683 seconds.
Remote result: 12/12 PASS, 4.031 seconds, in completed successful
[run 37847159751](https://github.com/Gutts15/Technology-Library-Ingestor/actions/runs/37847159751)
at `ff75420d9d8cd0fcc1e9cfe23e66bd668a8099bc`. All job steps, including clean
workspace verification, succeeded. Stage 21: IN_PROGRESS.

`tests/test_multimodal_evidence_integration.py` creates temporary synthetic raw
files, invokes the real ingestion CLIs and follows their compact evidence through
the actual envelope and candidate/receipt storage boundary. It does not inject
pre-extracted text in place of decoding.

| Raw input | Real extraction | Expected boundary behavior |
| --- | --- | --- |
| UTF-8 text | Text decoder | Candidate created once; repeat does not fetch or infer again. |
| PDF | pdftotext | Same behavior; subject and core capability retained. |
| DOCX / PPTX | Existing ZIP/XML parsers | Same behavior for each format. |
| CSV / XLSX | Existing CSV/openpyxl parsers | Same behavior for each format. |
| PNG with text | FFmpeg + Tesseract | Same behavior from actual OCR. |
| MP4 with text | FFmpeg frame extraction + Tesseract + compactor | Same behavior from actual decoded frames/OCR. |
| Empty text | Text decoder | HELD before source fetch or model call; zero candidates. |
| Image with OCR disabled | FFmpeg decoding | HELD; decoded pixels alone do not supply text evidence. |
| Blank video | FFmpeg + Tesseract + compactor | HELD; no invented text or candidate. |
| Audio without transcription | FFmpeg decoding/preview | HELD; audio metadata/preview alone cannot support a candidate. |

All twelve cases also require evidence <=3072 bytes, matching source/envelope
hashes via the existing bridge, no source-name/provider-ID canaries in decoding
logs or envelopes, and no canonical directory. Eight positive cases check one
candidate and repeat idempotency; four negative cases check zero source/model
calls and zero candidates. Fixtures and output live in a temporary directory and
are automatically removed. No media bytes are committed or uploaded as artifacts.

The complete 79,305-character CI job log was checked for the source-name,
provider-ID and source-text canaries, private Drive URLs, high-confidence
credentials/private keys, email and personal Windows-path patterns. No checked
matches were found. The run has zero artifacts. This covers this synthetic job
only; it does not prove privacy of future real-file processing.

Related local verification passed 54 contract/evidence/transport/preparation
tests, in addition to the twelve raw-file integration cases. The privacy gate
passed on 278 tracked files; cost guardrail passed on 170 checked files with zero
violations. Alignment passed with Charter 1.0, zero routine human steps and
black-box NOT_RUN. The tested tree was matched to the published tree before
local history reconciliation. Main and production schedules remain unchanged.

## Limits

The verified public-source probe and model response are deterministic stubs.
The test establishes actual decoding and wiring through the candidate/storage
boundary; it does not qualify model accuracy on these extracted inputs. The
separate frozen 30-case model and five-case live bridge proofs remain valid within
their own scope. No speech transcription, private Drive integration, actual
public-source retrieval, automatic canonical publication, recovery or representative daily-flow
acceptance is implied by this test. The tiny clean fixtures are not a capacity or
malformed/large-file robustness proof.

The new branch-scoped CI workflow has one standard ephemeral runner, a ten-minute
ceiling, read-only repository permission, no secrets, no production schedule and
no retained artifacts. It shares the existing serialized proof group. Normal
daily intake, legacy default model and main branch remain unchanged.

## Next checks

Qualify live model decisions on decoded evidence, actual speech extraction and
private storage/public-source boundaries after an allowed execution path is
established. Keep the original Charter and `EXECUTION_DECISIONS.md` constraints;
this proof introduces no provider account or production deployment requirement.
