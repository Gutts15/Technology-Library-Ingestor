# Stage 20 — Zero-touch Drive intake (implementation, not acceptance)

The source is only `99_INBOX/DROP_HERE` on the private `tl:` remote. A
default-branch GitHub Actions schedule polls at UTC minutes 17 and 47. The
workflow uses the existing `TL_RCLONE_CONFIG` secret, read-only GitHub
permission, no artifacts, and the same concurrency group as the existing
private-storage ingest workflow. It does not run in pull requests.

Before enabling the schedule, run its one-time `baseline_existing` dispatch on
reviewed `main`. This creates private revision receipts for items already in
DROP_HERE **without copying, routing, or processing them**. It refuses a second
baseline. The scheduled job remains inert until the repository variable
`TL_STAGE20_ENABLED` is `true`; the poller also refuses to run without the
private baseline marker. This prevents a newly installed schedule from
processing pre-Stage-20 real backlog. These are one-time deployment controls,
not routine user steps.

While Stage 20 cadence acceptance is underway, the deployed workflow also
passes `--fixture-only`: it selects only the explicitly named synthetic test
family and excludes **all** other items, including any new real upload. The
general discovery implementation remains available, but removing this safety
gate requires a separate reviewed change after acceptance.

Discovery lists only the inbox. Each source is keyed by the SHA-256 digest of
its provider ID; a missing provider ID is held rather than treating its name
as a stable identity. A revision digest uses size, modification time, and
available provider hashes. State is one JSON receipt per source, stored only
under the private Drive process log. Source names and IDs never enter public
state or normal logs. Files are copied to deterministic pseudonymous queue
names; the source remains in DROP_HERE so subsequent revisions are observable.

Each run enqueues at most five objects and 512 MiB, with a 512 MiB per-object
limit, a 15-minute processing budget, a 20-minute lease, and a 20-minute runner
timeout. Excess work is left for later runs. The lease records intent before
copy; after an interruption, the next eligible run checks the same destination
and records completion without making a duplicate copy. A source changed
during copy is held for a later revision. Idle runs only list the inbox and
write no private state.

A separate scheduled keepalive checks daily and makes a public **empty commit**
only if the latest repository commit is at least 30 days old. It has no Drive
secret, no private inputs, and no artifact; its write permission is the sole
narrow exception in the security gate. This mitigates GitHub's automatic
disabling of schedules after 60 days of public-repository inactivity. It is
not a guarantee against an extended GitHub scheduler outage or repository
policy blocking bot pushes, so cadence acceptance must verify both jobs.

The intake poller only places selected items into existing private media
queues. It does not change the Ingestor Core or authorize processing the
pre-Stage-20 backlog. Stage 20 is not DONE until controlled synthetic Drive
fixtures demonstrate repeated unattended scheduled runs, retries, revision
handling, and sanitized public logs. Oversized or unsupported provider objects
remain deferred/held and need a future bounded policy before universal intake
can be claimed.
