# Observability and cadence readiness

## Purpose

The ingest pipeline records only privacy-safe operational telemetry needed to tune workload limits, runtime and future scheduling. Observability must not become a second archive of private content.

The current flow is:

```text
one ingest workflow run
  -> src/run_report.py
  -> 99_INBOX/PROCESS_LOG/RUNS/run-<run-id>-attempt-<n>.json
  -> src/run_metrics.py
  -> 99_INBOX/PROCESS_LOG/METRICS/latest.json
  -> src/cadence_assess.py
  -> 99_INBOX/PROCESS_LOG/METRICS/cadence-readiness.json
```

All three steps are best-effort in the real ingest workflow. A telemetry failure must not turn successful ingestion into a failed job.

## Privacy boundary

Normal observability outputs may contain aggregate operational values such as:

```text
run duration
job status
planner heavy choice
planned/selected/processed/finalized/rejected/error counts
backlog counts and aggregate bytes
dependency groups used
pipeline counters
```

They must not copy:

```text
source filenames
package IDs
raw file IDs
URLs or query strings
OCR text
transcripts
sheet names or cell contents
image/audio/video content
arbitrary bridge result payloads
```

`run_metrics.py` does not trust input reports blindly. It re-sanitizes through an allow-list and ignores unknown fields even though `run_report.py` is already designed to emit sanitized reports.

No observability document is uploaded as a GitHub Actions artifact.

## Per-run report

Implementation:

```text
src/run_report.py
report version: 0.1.0
schema version: 1
```

Destination:

```text
99_INBOX/PROCESS_LOG/RUNS/run-<run-id>-attempt-<n>.json
```

The report combines the sanitized workload plan with aggregate bridge summaries. Package IDs/result arrays are discarded.

Important fields include:

```text
run.started_at
run.finished_at
run.duration_seconds
run.status
plan.heavy_choice
plan.batches
plan.queues
plan.totals
dependencies
pipelines
totals
```

The workflow starts timing immediately after checkout. `duration_seconds` therefore measures the controlled workflow portion, not every millisecond of GitHub runner lifecycle.

## Aggregate metrics

Implementation:

```text
src/run_metrics.py
metrics version: 0.1.0
schema version: 1
```

Default window:

```text
latest 50 run-*.json reports
hard configurable maximum: 200
```

Destination:

```text
99_INBOX/PROCESS_LOG/METRICS/latest.json
```

Only files matching `run-*.json` are candidates. Invalid schema/timestamp reports are skipped and counted rather than poisoning the whole aggregate.

The aggregate includes:

```text
window
  reports_seen
  reports_used
  invalid_reports
  started_at / finished_at

runs
  status counts
  work runs / empty plans
  error-run rate
  duration sum / mean / median / p90 / max
  observed_runtime_minutes

workload
  planned / selected / processed / finalized / rejected / errors / warnings
  handled
  selection ratio
  handled ratio
  handled per observed minute

backlog_at_plan
  mean / max / latest for supported, unsupported and raw backlog

heavy_choice
  audio / video / none / unknown counts

dependencies_runs
  rclone / ffmpeg / tesseract / poppler / transcription / spreadsheet groups

pipelines
  per-pipeline aggregate counters
```

Duration is re-derived from `started_at` and `finished_at`; the aggregator does not trust a supplied duration field.

### Runtime metric is not billing

`observed_runtime_minutes` is the sum of observed workflow duration divided by 60. It is **not** a claim about GitHub Actions billable-minute rounding, included quota or monetary cost. Billing/cost decisions must use current GitHub billing information separately.

## Cadence readiness

Implementation:

```text
src/cadence_assess.py
assessment version: 0.1.0
schema version: 1
```

Destination:

```text
99_INBOX/PROCESS_LOG/METRICS/cadence-readiness.json
```

This component deliberately does **not** create a schedule and does **not** recommend an exact cron expression.

Its job is only to decide whether the real telemetry sample is mature enough for a human cadence review.

### Conservative readiness policy V1

Minimum representative sample:

```text
10 total runs
5 runs with planned work
3 heavy runs (audio + video)
```

Stability limits:

```text
invalid report rate <= 10%
error run rate <= 20%
p90 controlled duration <= 1500 seconds (25 minutes)
```

The real ingest job timeout remains 1800 seconds (30 minutes), so the p90 threshold intentionally leaves a five-minute buffer.

States:

```text
insufficient_data
  sample is not yet representative

hold
  sample is large enough but stability limits are violated

ready_for_cadence_review
  sample and stability gates pass
```

Additional non-blocking signals:

```text
empty_run_waste_signal
  low       < 25%
  moderate  25% to < 50%
  high      >= 50%

backlog_present
  true when latest supported pending backlog > 0
```

A high empty-run signal is evidence that a future polling schedule may be too frequent. It is not permission for the system to change its own schedule.

## Trigger policy

The main ingest workflow currently supports:

```text
workflow_dispatch  # manual
workflow_call      # reusable
```

It deliberately does not contain `schedule:`.

The bootstrap wiring smoke test protects this invariant. Scheduling should only be added after:

1. a functional real private storage backend exists;
2. sufficient real telemetry has accumulated;
3. `cadence-readiness.json` reaches `ready_for_cadence_review`;
4. current hosted-runner quota/cost has been checked;
5. a cadence is explicitly chosen and reviewed.

## Validation state

Synthetic tests currently validate:

```text
per-run report sanitization and remote persistence
aggregate statistics and strict field whitelist
local and fake-rclone aggregation
invalid-report isolation
empty-history behavior
runtime re-derivation from timestamps
privacy canaries across aggregation
cadence states: insufficient / ready / hold
high empty-run waste signal
cadence remote persistence
missing-metrics safe failure
workflow ordering:
  telemetry -> metrics -> cadence -> cleanup
best-effort behavior
no active schedule trigger
Security Gate compatibility
```

Real quota/cadence tuning remains intentionally blocked until a private storage backend is functional and produces representative real run history.
