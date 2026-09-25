# Architecture v1

## Purpose

Pre-process large/private inputs into compact, auditable and selectively retrievable evidence packages before any LLM or agent performs semantic analysis.

Storage is infrastructure, not part of the processing contract. The user-facing intake is one folder.

## User-facing contract

```text
user
  -> 99_INBOX/DROP_HERE
  -> system handles classification, planning, processing, logging, recovery and routing
```

Manual actions are acceptable during development and review. They are not part of the target production workflow.

## Current flow

```text
Private storage via rclone
  99_INBOX/DROP_HERE
      |
      v
storage_preflight.py
      |
      v
reconcile_queues.py
      |  queue-aware retry/finalization
      |  VIDEO / IMAGE / AUDIO / LINK / TEXT / DOCUMENTS / SPREADSHEETS
      v
media_router.py
      |
      +-- video       -> TO_REVIEW/VIDEOS
      +-- image       -> TO_REVIEW/IMAGES
      +-- audio       -> TO_REVIEW/AUDIO
      +-- document    -> TO_REVIEW/DOCUMENTS
      +-- spreadsheet -> TO_REVIEW/SPREADSHEETS
      +-- text        -> TO_REVIEW/TEXT
      +-- link        -> TO_REVIEW/LINKS
      +-- archive     -> TO_REVIEW/ARCHIVES
      +-- unknown     -> ERROR/UNSORTED
      |
      +-- collision -> deterministic safe rename, never overwrite
      v
workload_plan.py
      |
      +-- count supported executable backlog
      +-- retain unsupported counts without waking processors
      +-- budget cheap/moderate queues
      +-- choose only AUDIO or VIDEO as heavy work for this run
      +-- export TL_PLAN_* batch limits
      v
pipeline dispatch
      |
      +-- LINK ---------------------> link_bridge.py -> link_ingest.py
      |                                  -> local parse only
      |                                  -> no arbitrary network fetch
      |
      +-- IMAGE --------------------> image_bridge.py -> image_ingest.py
      |
      +-- AUDIO --------------------> audio_bridge.py -> audio_ingest.py
      |                                  -> time-spread preview.opus
      |                                  -> optional faster-whisper transcript
      |
      +-- VIDEO --------------------> rclone_bridge.py -> batch_ingest.py -> ingest_video.py
      |
      +-- TEXT/PDF/DOCX/PPTX ------> text_document_bridge.py -> text_document_ingest.py
      |
      +-- XLSX/XLSM/CSV/TSV -------> spreadsheet_bridge.py -> spreadsheet_ingest.py
      |
      +-- ARCHIVES / unsupported ---> preserved until explicit safe handling exists
      v
READY_FOR_ANALYSIS/<package-id>/
PROCESS_LOG/RECORDS/<package-id>.json
      |
      +-- success -> PROCESSED/<type>
      +-- deterministic unsafe link -> ERROR/LINKS, non-retryable
      +-- retryable failure -> reconciliation path
      +-- exhausted failure -> ERROR/<type>
      v
retention_cleanup.py
      |
      +-- strict pipeline package contract
      +-- PROCESSED -> RETENTION_TRASH -> purge
      +-- destructive mode not enabled in real workflow yet
      v
Technology Library semantic curation
```

## Routing model

Routing is mechanical, never semantic.

Priority:

1. strong MIME families and specific application MIME types;
2. known specific filename extension;
3. broad/generic MIME families;
4. `unknown` when none is sufficient.

Specific extensions are evaluated before generic labels such as `application/zip` or `text/plain`. OOXML formats are ZIP containers, so `.xlsx`, `.docx` and `.pptx` must not become `ARCHIVE` merely because a provider exposes generic ZIP MIME.

Current categories:

```text
video
image
audio
document
spreadsheet
text
link
archive
unknown
```

Code/source files route to `TEXT`. `.url` and `.webloc` route to `LINKS`, including MIME-only `application/internet-shortcut`, `application/x-url` and `application/x-webloc` inputs.

Unknown inputs are preserved under `ERROR/UNSORTED`.

## Internal queue semantics

```text
99_INBOX/DROP_HERE

99_INBOX/TO_REVIEW/VIDEOS
99_INBOX/TO_REVIEW/IMAGES
99_INBOX/TO_REVIEW/AUDIO
99_INBOX/TO_REVIEW/DOCUMENTS
99_INBOX/TO_REVIEW/SPREADSHEETS
99_INBOX/TO_REVIEW/TEXT
99_INBOX/TO_REVIEW/LINKS
99_INBOX/TO_REVIEW/ARCHIVES

99_INBOX/READY_FOR_ANALYSIS
99_INBOX/PROCESS_LOG/RECORDS
99_INBOX/PROCESS_LOG/RETRY

99_INBOX/PROCESSED/<type>
99_INBOX/RETENTION_TRASH
99_INBOX/ERROR/<type>
99_INBOX/ERROR/UNSORTED
```

`DROP_HERE` is the stable external contract. The queues are implementation details.

## Versioning

```text
Video core pipeline       0.5.0
Video evidence compactor  0.6.2
Transcript schema         2
Image pipeline            0.1.0
Image summary             0.1.0
Audio pipeline            0.1.0
Audio summary             0.1.0
Link pipeline             0.1.0
Link summary              0.1.0
Text/document pipeline    0.1.0
Text/document summary     0.1.0
Spreadsheet pipeline      0.1.0
Spreadsheet summary       0.1.0
Workload planner          0.1.1
Process record            independently versioned
```

Expensive extraction checkpoints are versioned separately from compact-evidence formats so a packaging improvement does not automatically force full reprocessing.

## Workload Planner V1

`workload_plan.py` runs after routing and before processor dispatch. It reads only rclone queue metadata and writes a local, privacy-safe execution plan.

Default run budget:

```text
LINKS        <= 20
IMAGES       <= 10
TEXT + DOCS  <= 10 combined
SPREADSHEETS <= 5

HEAVY
AUDIO        <= 1
or
VIDEO        <= 3
```

Only one transcription-heavy queue runs per workflow execution. If both AUDIO and VIDEO have supported pending items, the queue whose oldest supported item has waited longest wins that run.

The planner reuses support definitions exposed by the real bridges/processors. Routed-but-unsupported files such as WMA, GIF/TIFF, DOC/PPT and XLS/ODS are counted as `preserved_unsupported` and do not generate executable batch work.

Plan artifacts stay local to the runner:

```text
tl-workload-plan.json
TL_PLAN_LINK_BATCH
TL_PLAN_IMAGE_BATCH
TL_PLAN_AUDIO_BATCH
TL_PLAN_VIDEO_BATCH
TL_PLAN_CONTENT_BATCH
TL_PLAN_SPREADSHEET_BATCH
TL_PLAN_HEAVY_KIND
```

The JSON contains aggregate counts/bytes, preserved unsupported counts and chosen batches. It contains no private filenames. `TL_PLAN_* = 0` causes the corresponding GitHub Actions step to be skipped.

The planner is deliberately not a runtime estimator. It does not infer media duration from byte size or filename. Its job is deterministic load control and fairness, not pretending metadata can predict CPU time perfectly.

## Video pipeline

Current video processing includes SHA-256, ffprobe metadata, FFmpeg scene detection and fallback, perceptual dHash dedupe, temporal keyframes, contact sheet, optional Tesseract OCR, optional faster-whisper CPU/int8 transcription, decode-quality metrics, `timeline.md`, <=3 KB compact evidence, bounded batch processing and checkpoint/resume.

The Whisper model is cached in memory per Python process.

## Image pipeline V1

Supported:

```text
JPEG / JPG
PNG
WebP
BMP
```

`GIF`, `TIFF`, `HEIC` and `HEIF` remain preserved in `TO_REVIEW/IMAGES`.

Package:

```text
ingest.json
checkpoint.json
image-index.json
evidence-summary.json
process-record.json
preview.jpg
ocr.json                 # optional
```

Image V1 uses ffprobe, FFmpeg and optional Tesseract. Embedded metadata is not exported wholesale, `preview.jpg` is regenerated without metadata, and the original filename stays out of compact evidence/logs.

Safety bounds include 100 MB input, 60M decoded pixels, 30k px per dimension and <=3 KB compact summary. `preview.jpg` is required evidence.

## Audio pipeline V1

Supported:

```text
MP3
M4A
AAC
WAV
FLAC
OGG
OPUS
```

`WMA` is preserved without processing.

Package:

```text
ingest.json
checkpoint.json
audio-index.json
evidence-summary.json
process-record.json
preview.opus
transcript.md            # optional
transcript.json          # optional
```

Audio and speech are separate evidence classes. `preview.opus` is mandatory because transcript text cannot represent music, ambience or sound effects.

Preview policy:

```text
<= 60 seconds
  -> normalized full audio

> 60 seconds
  -> up to 20 s beginning
  -> up to 20 s middle
  -> up to 20 s end
```

Preview is mono, 16 kHz, Opus ~24 kbps, metadata stripped.

Automatic transcription is limited to 30 minutes per file. Above that, ingest and preview continue but transcription is skipped. The workflow uses Whisper `base`; the Workload Planner allows at most one audio item in an audio-heavy run and never schedules AUDIO and VIDEO heavy work together.

## Link pipeline V1

Link V1 is intentionally offline-safe.

Supported shortcut files:

```text
.url
.webloc
```

`.url` is parsed as an Internet Shortcut. `.webloc` is parsed with Python stdlib `plistlib`, supporting XML or binary plist.

No network request occurs during ingestion. The package records:

```text
network.fetched = false
network.policy = offline_v1
```

Accepted URL schemes:

```text
http
https
```

Deterministic rejection rules include:

```text
file://
javascript:
data:
unsupported/missing scheme
missing/invalid host
invalid port
userinfo such as user:password@host
malformed shortcut
```

These cases move to `ERROR/LINKS`, receive a process record with `retryable=false`, and count as `rejected` rather than failing the whole batch.

Valid link package:

```text
ingest.json
checkpoint.json
link-index.json
evidence-summary.json
process-record.json
```

`link-index.json` is private and may retain the full URL. Compact evidence carries a URL without query string or fragment, plus bounded host/scheme indicators. This keeps common tracking parameters, signed query values and accidental tokens out of normal curator context.

Link V1 input is capped at 1 MB per shortcut and 8192 characters per URL. Compact summary is capped at 3 KB.

## Text/document pipeline V1

Supported extraction:

```text
TEXT / CODE -> local decoding
PDF         -> pdftotext -layout
DOCX        -> stdlib ZIP/XML
PPTX        -> stdlib ZIP/XML, slide order preserved
```

Package:

```text
ingest.json
checkpoint.json
content-index.json
evidence-summary.json
process-record.json
chunks/
```

Large text/document content is chunked so the consumer can retrieve only relevant ranges.

## Spreadsheet pipeline V1

Supported:

```text
XLSX
XLSM
CSV
TSV
```

`.xls` and `.ods` remain preserved without processing.

Pinned dependencies:

```text
openpyxl==3.1.5
et_xmlfile==2.0.0
defusedxml==0.7.1
```

Package:

```text
ingest.json
checkpoint.json
spreadsheet-index.json
evidence-summary.json
process-record.json
sheets/<sheet-id>/chunk-XXXX.jsonl
```

The processor preserves sheet order/visibility, original row numbers, sparse cells, formulas, cached values when available, structural counts and row-range chunks. Default chunking is 1000 nonempty rows with a 250 MB input cap.

## Package completeness contracts

Base package:

```text
ingest.json
checkpoint.json
evidence-summary.json
process-record.json
```

Strict additions:

```text
TO_REVIEW/VIDEOS
  -> base

TO_REVIEW/IMAGES
  -> base + image-index.json + preview.jpg

TO_REVIEW/AUDIO
  -> base + audio-index.json + preview.opus

TO_REVIEW/TEXT
  -> base + content-index.json

TO_REVIEW/DOCUMENTS
  -> base + content-index.json

TO_REVIEW/SPREADSHEETS
  -> base + spreadsheet-index.json

TO_REVIEW/LINKS
  -> base + link-index.json
```

Unknown queues return no inferred weaker contract. Reconciliation and retention preserve/guard instead.

Examples:

```text
missing preview.jpg
missing preview.opus
missing spreadsheet-index.json
missing link-index.json

-> incomplete
-> cannot be finalized by reconciliation
-> cannot enter automatic retention
```

## PROCESS_LOG contract

For completed packages:

```text
READY_FOR_ANALYSIS/<package-id>/process-record.json
PROCESS_LOG/RECORDS/<package-id>.json
```

Records contain source identity, provider, timestamps, pipeline versions, destination, retryability and retention metadata.

Deterministically rejected links are also recorded centrally, but do not create successful READY packages.

## Queue-aware reconciliation

Known mappings:

```text
TO_REVIEW/VIDEOS       -> PROCESSED/VIDEOS       / ERROR/VIDEOS
TO_REVIEW/IMAGES       -> PROCESSED/IMAGES       / ERROR/IMAGES
TO_REVIEW/AUDIO        -> PROCESSED/AUDIO        / ERROR/AUDIO
TO_REVIEW/LINKS        -> PROCESSED/LINKS        / ERROR/LINKS
TO_REVIEW/TEXT         -> PROCESSED/TEXT         / ERROR/TEXT
TO_REVIEW/DOCUMENTS    -> PROCESSED/DOCUMENTS    / ERROR/DOCUMENTS
TO_REVIEW/SPREADSHEETS -> PROCESSED/SPREADSHEETS / ERROR/SPREADSHEETS
```

Rules:

```text
missing central process record
-> restore from READY_FOR_ANALYSIS

retryable failure
-> requeue to its own original queue

non-retryable rejection
-> leave in matching ERROR queue
-> do not retry

source_move_failed + complete package
-> finish only source movement
-> do not rerun extraction

source_move_failed + incomplete package
-> retry/requeue while preserving source

retry count >= 3
-> mark exhausted
-> preserve in matching ERROR queue
```

## File identity

Primary identity order:

1. provider File ID when available;
2. deterministic fallback identity;
3. source SHA-256 for byte identity/checkpoint validation;
4. filename as display/provenance metadata only.

Package directories use a 20-character SHA-256-derived identifier.

## Retention

Retention is quarantine-first:

```text
PROCESSED/<type>
  -> retention deadline
  -> package contract validation
  -> RETENTION_TRASH
  -> 7-day grace window
  -> purge
```

Initial policy:

- `KEEP_ORIGINAL=true`: never removed automatically;
- recoverable source: 7-day default;
- nonrecoverable/unknown: 30-day default;
- ERROR: excluded from cleanup;
- incomplete package: cleanup blocked;
- unknown package contract: cleanup blocked;
- invalid metadata: cleanup blocked.

`retention_cleanup.py` is dry-run by default. Destructive cleanup is not enabled in the real workflow until real-storage validation.

## Storage abstraction

The real workflow uses an rclone remote named `tl:`.

Supported configuration paths:

1. generic `TL_RCLONE_CONFIG` + `TL_STORAGE_PROVIDER`;
2. legacy Google Drive secrets as compatibility fallback.

Processing logic is structurally independent of Google Drive.

## Credential and network boundaries

Storage credentials belong to storage-aware code only.

```text
GitHub secret
  -> temporary rclone config
       -> preflight / reconciliation / router / planner / bridges
            -> local processors receive sanitized local files
            -> storage credentials are removed before processor work
```

`audio_bridge.py` processes in-process to reuse the Whisper cache but removes rclone credential variables from the normal environment first.

`link_ingest.py` has a separate invariant: it performs no external network request at all. Network enrichment belongs to a later, explicit curation/research layer rather than being an ingestion side effect.

## Security invariants

- no credentials committed;
- no private raw input or generated private package committed;
- no private workflow artifacts uploaded;
- minimum GitHub permissions;
- third-party Actions pinned to immutable SHA;
- private filenames/content excluded from normal logs and workload plan;
- image/audio previews strip inherited metadata;
- link queries/fragments excluded from compact evidence;
- Link V1 performs no arbitrary fetch;
- routing never overwrites destination data;
- unsupported data is preserved rather than discarded;
- unsupported data does not wake a processor without executable work;
- deterministic unsafe links are rejected without retry;
- retry is bounded for transient failures;
- only one transcription-heavy queue is scheduled per run;
- finalization requires a complete package by queue-specific contract;
- destructive cleanup requires a complete package, quarantine and explicit apply mode;
- workspace must be clean after CI tests.

## Current validation state

Validated with real private media:

- one video;
- batch of five videos.

Validated synthetically end-to-end:

- unified `DROP_HERE` routing;
- generic-MIME vs specific-extension disambiguation;
- Image V1 and lifecycle;
- Audio V1, distributed preview, real `tiny` transcription and lifecycle;
- Link V1 `.url/.webloc`, offline policy, private-query redaction, deterministic rejection and lifecycle;
- PROCESS_LOG;
- queue-aware retry/reconciliation for VIDEO/IMAGE/AUDIO/LINK/TEXT/DOCUMENTS/SPREADSHEETS;
- quarantine/purge retention lifecycle;
- Markdown/source code, PDF, DOCX and PPTX ingestion;
- XLSX/XLSM/CSV/TSV selective extraction;
- compact evidence and indexes;
- privacy/log hygiene;
- Whisper model cache regression tests;
- Workload Planner V1 heavy fairness and batch limits;
- unsupported WMA/GIF/DOC/XLS preservation without processor wakeup;
- `TL_PLAN_*` integration into the private ingest workflow;
- Security Gate after Workload Planner V1 integration.

## Next architectural milestone

Do not add archive extraction merely because `ARCHIVES` exists as a queue. ZIP/RAR/7z processing introduces a different threat model: decompression bombs, path traversal, nested archives, encrypted payloads and extreme expansion ratios.

The next milestone is **bootstrap/runtime-cost reduction before zero-touch scheduling**:

```text
checkout
  -> install only minimum storage tooling
  -> preflight / reconcile / route / plan
  -> if there is executable work:
       install only dependencies required by that plan
       run selected pipelines
  -> otherwise finish quickly
```

This matters because the current workflow still installs FFmpeg, Poppler, Tesseract, faster-whisper and spreadsheet dependencies before it knows whether those tools are needed.

After that:

1. prepare zero-touch execution without enabling a schedule while no private backend is functional and quota/cost behavior is unvalidated;
2. validate the multi-pipeline flow on a functional real private storage backend;
3. run scale tests, including larger video batches and very large spreadsheets;
4. build semantic Technology Library curation / SOURCE registry;
5. evaluate archive and legacy-format support only with explicit safety limits;
6. audit security/history before any publication decision.